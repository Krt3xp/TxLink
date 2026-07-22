from __future__ import annotations

import hashlib
import logging
import os
import posixpath
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

from taxlink_nfse.config import SyncConfig
from taxlink_nfse.storage import SqliteRepository


class MirrorSyncService:
    def __init__(
        self,
        master_database_path: Path,
        config: SyncConfig,
        repository: SqliteRepository,
    ):
        self.master_database_path = master_database_path
        self.config = config
        self.repository = repository
        self._execution_lock = threading.Lock()
        self.logger = logging.getLogger(__name__)

    def enqueue(self, trigger_source: str) -> str:
        if not self.config.enabled:
            raise RuntimeError("A sincronizacao SFTP esta desabilitada.")
        return self.repository.create_sync_run(trigger_source)

    def execute(self, sync_id: str) -> bool:
        if not self._execution_lock.acquire(blocking=False):
            return False
        try:
            if not self.repository.claim_sync_run(sync_id):
                return False
            mirror_path, size_bytes, digest = self.create_consistent_snapshot()
            remote_path = self.transfer_atomic(mirror_path)
            self.repository.finish_sync_run(
                sync_id,
                str(mirror_path),
                remote_path,
                size_bytes,
                digest,
            )
            return True
        except Exception as exc:
            self.logger.exception("Falha na sincronizacao do espelho %s", sync_id)
            self.repository.fail_sync_run(sync_id, str(exc))
            return False
        finally:
            self._execution_lock.release()

    def execute_next(self) -> str | None:
        sync_id = self.repository.next_queued_sync_run()
        if sync_id is None:
            return None
        self.execute(sync_id)
        return sync_id

    def create_consistent_snapshot(self) -> tuple[Path, int, str]:
        if not self.master_database_path.is_file():
            raise FileNotFoundError(f"Banco master nao encontrado: {self.master_database_path}")
        mirror_path = self.config.local_mirror_path
        mirror_path.parent.mkdir(parents=True, exist_ok=True)
        building_path = mirror_path.with_name(f".{mirror_path.name}.building")
        building_path.unlink(missing_ok=True)
        source_uri = f"file:{self.master_database_path.as_posix()}?mode=ro"
        try:
            with closing(sqlite3.connect(source_uri, uri=True, timeout=30)) as source:
                with closing(sqlite3.connect(building_path)) as destination:
                    source.backup(destination)
                    result = destination.execute("PRAGMA integrity_check").fetchone()
                    if result is None or str(result[0]).lower() != "ok":
                        raise RuntimeError(f"Falha no integrity_check do espelho: {result}")
                    destination.commit()
            os.replace(building_path, mirror_path)
        finally:
            building_path.unlink(missing_ok=True)
        return mirror_path, mirror_path.stat().st_size, _sha256_file(mirror_path)

    def transfer_atomic(self, mirror_path: Path) -> str:
        try:
            import paramiko
        except ImportError as exc:
            raise RuntimeError("Instale paramiko para realizar a sincronizacao SFTP.") from exc

        if not self.config.known_hosts_path.is_file():
            raise RuntimeError(
                f"Arquivo known_hosts nao encontrado: {self.config.known_hosts_path}"
            )
        password = (
            os.environ.get(self.config.password_env) if self.config.password_env else None
        )
        key_password = (
            os.environ.get(self.config.private_key_password_env)
            if self.config.private_key_password_env
            else None
        )
        remote_temp = posixpath.join(
            self.config.remote_directory, self.config.temporary_filename
        )
        remote_final = posixpath.join(
            self.config.remote_directory, self.config.final_filename
        )
        client = paramiko.SSHClient()
        client.load_host_keys(str(self.config.known_hosts_path))
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            client.connect(
                hostname=self.config.host,
                port=self.config.port,
                username=self.config.username,
                password=password,
                key_filename=(
                    str(self.config.private_key_path)
                    if self.config.private_key_path is not None
                    else None
                ),
                passphrase=key_password,
                timeout=self.config.timeout_seconds,
                auth_timeout=self.config.timeout_seconds,
                banner_timeout=self.config.timeout_seconds,
                look_for_keys=False,
                allow_agent=False,
            )
            with client.open_sftp() as sftp:
                sftp.put(str(mirror_path), remote_temp, confirm=True)
                remote_size = int(sftp.stat(remote_temp).st_size)
                if remote_size != mirror_path.stat().st_size:
                    raise RuntimeError(
                        f"Tamanho SFTP divergente: local={mirror_path.stat().st_size} "
                        f"remoto={remote_size}"
                    )
                try:
                    sftp.posix_rename(remote_temp, remote_final)
                except OSError as exc:
                    raise RuntimeError(
                        "O servidor SFTP nao aceitou a renomeacao atomica posix-rename."
                    ) from exc
        finally:
            client.close()
        return remote_final


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
