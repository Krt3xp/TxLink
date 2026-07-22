# Implantacao Ubuntu e espelho SFTP

## Estrutura sugerida

```text
/opt/taxlink-collector/                 aplicacao e venv
/etc/taxlink/config.toml                configuracao sem senhas
/etc/taxlink/taxlink-collector.env      tokens e senhas
/etc/taxlink/certificates/              certificado A1
/etc/taxlink/ssh/known_hosts            chave publica do Windows
/var/lib/taxlink/taxlink-nfse.sqlite3   banco master
/var/lib/taxlink/taxlink_mirror.db      ultimo snapshot consistente
/var/log/taxlink/                        logs
```

O usuario `taxlink` deve possuir leitura do certificado e escrita apenas nos
diretorios de dados e logs. Para PFX, a senha e indicada por `password_env`. A
senha em si fica no arquivo de ambiente com permissao `0600`.

## API e servico

```bash
python3.11 -m venv /opt/taxlink-collector/.venv
/opt/taxlink-collector/.venv/bin/pip install -e /opt/taxlink-collector
sudo cp packaging/taxlink-collector.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now taxlink-collector
sudo systemctl status taxlink-collector
```

Exemplo de `/etc/taxlink/taxlink-collector.env`:

```text
TAXLINK_API_TOKEN=gere-um-token-longo-e-aleatorio
TAXLINK_NFSE_PFX_PASSWORD_HUGOL=senha-do-pfx
TAXLINK_SFTP_PASSWORD=senha-da-conta-sftp
```

## Windows OpenSSH

O Windows Server deve possuir o recurso OpenSSH Server, uma conta SFTP com
permissao de escrita no diretorio do espelho e a porta 22 liberada somente para
o IP do Ubuntu. Cadastre a chave do host no Ubuntu antes de ativar o sync:

```bash
ssh-keyscan -H windows-servidor.exemplo.local | sudo tee /etc/taxlink/ssh/known_hosts
```

Confirme por SFTP qual caminho representa o diretorio do sistema PHP. Dependendo
da configuracao do OpenSSH para Windows, ele pode aparecer como `/C:/TaxLink/data`
ou como um caminho relativo ao `ChrootDirectory`.

O envio usa a seguinte sequencia:

1. backup nativo do SQLite master para um arquivo local temporario;
2. `PRAGMA integrity_check`;
3. substituicao do espelho local;
4. upload para `taxlink_temp.db`;
5. conferencia do tamanho remoto;
6. `posix-rename` para `taxlink_mirror.db`.

Se o servidor nao oferecer a extensao `posix-rename`, a sincronizacao falha sem
remover o espelho anterior. O erro fica em `sync_run` para nova tentativa.

## Contratos

Nesta fase nao existe `CONTRATO_REFERENCIA`. As colunas `invoice.contract_id` e
`invoice.contract_number` continuam opcionais e novas notas sao gravadas com
ambas nulas.
