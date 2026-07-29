from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

from taxlink_nfse.collector import Collector
from taxlink_nfse.config import AppConfig
from taxlink_nfse.instance_lock import AlreadyRunningError, InstanceLock
from taxlink_nfse.logging_setup import configure_logging
from taxlink_nfse.storage import SqliteRepository
from taxlink_nfse.transport import MutualTlsTransport


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="taxlink-nfse",
        description="Coletor de NFS-e do Ambiente de Dados Nacional",
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("TAXLINK_NFSE_CONFIG", "config.toml"),
        help="Arquivo TOML de configuracao",
    )
    parser.add_argument("--verbose", action="store_true", help="Habilita logs detalhados")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db", help="Cria ou atualiza o banco SQLite")
    once = commands.add_parser("once", help="Executa um ciclo de coleta")
    once.add_argument("--force", action="store_true", help="Ignora next_poll_at")
    backfill = commands.add_parser(
        "backfill", help="Reprocessa o historico por NSU sem duplicar documentos"
    )
    backfill.add_argument("--unit", required=True, help="Codigo da unidade")
    backfill.add_argument("--from-nsu", type=int, default=1, help="NSU inicial")
    commands.add_parser("run", help="Executa continuamente em segundo plano")
    commands.add_parser("serve", help="Inicia API REST, scheduler e worker")
    commands.add_parser("sync", help="Gera e envia o espelho SQLite por SFTP")
    commands.add_parser("status", help="Exibe cursores e totais do coletor")
    commands.add_parser("doctor", help="Valida configuracao, banco e certificados")
    monitor_data = commands.add_parser(
        "monitor-data", help="Fornece dados JSON para o monitor local"
    )
    monitor_data.add_argument("--limit", type=int, default=500)
    return parser


def main(argv: list[str] | None = None) -> None:
    raise SystemExit(run(argv))


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = AppConfig.load(args.config)
        configure_logging(config.collector.log_path, args.verbose)
        repository = SqliteRepository(config.collector.database_path)

        if args.command == "init-db":
            repository.initialize(config.units)
            print(f"Banco inicializado: {config.collector.database_path}")
            return 0
        if args.command == "status":
            repository.initialize(config.units)
            print(repository.dump_status_json())
            return 0
        if args.command == "doctor":
            return _doctor(config, repository)
        if args.command == "monitor-data":
            if not config.collector.database_path.is_file():
                repository.initialize(config.units)
            lock_path = config.collector.database_path.with_suffix(".lock")
            print(
                json.dumps(
                    {
                        "collector_running": _collector_is_running(lock_path),
                        "database_path": str(config.collector.database_path),
                        "log_path": str(config.collector.log_path),
                        "status": repository.status(),
                        "invoices": repository.invoice_summaries(args.limit),
                        "runs": repository.recent_runs(100),
                        "certificates": repository.certificates(),
                        "jobs": repository.recent_collection_jobs(20),
                        "sync_runs": repository.recent_sync_runs(20),
                    },
                    # Mantem caminhos Windows com acentos seguros mesmo quando o
                    # PowerShell hospedeiro usa uma pagina de codigo diferente.
                    ensure_ascii=True,
                )
            )
            return 0

        lock_path = config.collector.database_path.with_suffix(".lock")
        with InstanceLock(lock_path):
            collector = Collector(config, repository=repository)
            if args.command == "once":
                summary = collector.run_cycle(force=bool(args.force))
                print(json.dumps(summary.as_dict(), ensure_ascii=False))
                return 1 if summary.errors else 0
            if args.command == "run":
                try:
                    collector.run_forever()
                except KeyboardInterrupt:
                    print("Coletor interrompido.")
                return 0
            if args.command == "serve":
                try:
                    import uvicorn
                except ImportError as exc:
                    raise RuntimeError("Instale uvicorn para iniciar a API.") from exc
                from taxlink_nfse.api import create_app

                uvicorn.run(
                    create_app(config),
                    host=config.api.host,
                    port=config.api.port,
                    workers=1,
                )
                return 0
            if args.command == "sync":
                from taxlink_nfse.sync import MirrorSyncService

                service = MirrorSyncService(
                    config.collector.database_path, config.sync, repository
                )
                sync_id = service.enqueue("CLI")
                success = service.execute(sync_id)
                print(json.dumps(repository.sync_run(sync_id), ensure_ascii=False))
                return 0 if success else 1
            if args.command == "backfill":
                unit = next((item for item in config.units if item.code == args.unit), None)
                if unit is None:
                    raise ValueError(f"Unidade nao encontrada: {args.unit}")
                if args.from_nsu < 0:
                    raise ValueError("--from-nsu nao pode ser negativo")
                selected_config = replace(config, units=(unit,))
                collector = Collector(selected_config, repository=repository)
                collector.initialize()
                target_nsu = repository.prepare_backfill(unit.code, args.from_nsu)
                if args.from_nsu >= target_nsu:
                    print(
                        json.dumps(
                            {
                                "unit": unit.code,
                                "from_nsu": args.from_nsu,
                                "target_nsu": target_nsu,
                                "status": "NOTHING_TO_DO",
                            }
                        )
                    )
                    return 0

                total_batches = 0
                total_received = 0
                total_stored = 0
                for _ in range(1000):
                    before = int(repository.cursor(unit.code)["next_nsu"])
                    summary = collector.run_cycle(force=True)
                    after = int(repository.cursor(unit.code)["next_nsu"])
                    total_batches += summary.batches_requested
                    total_received += summary.documents_received
                    total_stored += summary.documents_stored
                    if summary.errors or after <= before or after >= target_nsu:
                        break
                final_nsu = int(repository.cursor(unit.code)["next_nsu"])
                completed = final_nsu >= target_nsu
                if completed:
                    repository.complete_backfill(unit.code)
                print(
                    json.dumps(
                        {
                            "unit": unit.code,
                            "from_nsu": args.from_nsu,
                            "target_nsu": target_nsu,
                            "final_nsu": final_nsu,
                            "batches_requested": total_batches,
                            "documents_received": total_received,
                            "documents_stored": total_stored,
                            "status": "COMPLETED" if completed else "INCOMPLETE",
                        }
                    )
                )
                return 0 if completed else 1
        return 0
    except AlreadyRunningError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1


def _doctor(config: AppConfig, repository: SqliteRepository) -> int:
    failures: list[str] = []
    repository.initialize(config.units)
    print(f"OK banco SQLite: {config.collector.database_path}")
    transport = MutualTlsTransport()
    for unit in config.units:
        if not unit.enabled:
            print(f"IGNORADA unidade desabilitada: {unit.code}")
            continue
        try:
            certificate = repository.certificate_for_unit(unit.code)
            exists = transport.certificate_exists(certificate)
        except Exception as exc:
            exists = False
            failures.append(f"{unit.code}: erro ao verificar certificado: {exc}")
        if exists:
            print(f"OK certificado: {unit.code} ({certificate.provider})")
        else:
            failures.append(f"{unit.code}: certificado nao encontrado ou sem chave privada")

    for failure in failures:
        print(f"FALHA {failure}", file=sys.stderr)
    return 1 if failures else 0


def _collector_is_running(lock_path: Path) -> bool:
    try:
        with InstanceLock(lock_path):
            return False
    except AlreadyRunningError:
        return True


if __name__ == "__main__":
    main()
