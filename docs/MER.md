# MER atualizado — TaxLink NFS-e Collector

Versao do modelo: **4**
Banco fisico: **SQLite** (`data/taxlink-nfse.sqlite3`)

## 1. Modelo fisico do coletor

```mermaid
erDiagram
    SCHEMA_VERSION {
        INTEGER version PK
        TEXT applied_at
    }

    FISCAL_UNIT {
        INTEGER id PK
        TEXT code UK
        INTEGER system_unit_id "referencia logica PHP"
        TEXT tax_id "CNPJ da unidade"
        TEXT name
        TEXT environment
        TEXT certificate_provider
        TEXT certificate_reference
        INTEGER enabled
        TEXT created_at
        TEXT updated_at
    }

    DIGITAL_CERTIFICATE {
        INTEGER id PK
        INTEGER unit_id FK
        TEXT provider "pfx, pem ou windows legado"
        TEXT certificate_path
        TEXT private_key_path
        TEXT password_env "nome da variavel; nunca a senha"
        TEXT thumbprint
        TEXT certificate_tax_id
        TEXT valid_from
        TEXT valid_until
        INTEGER enabled
    }

    DISTRIBUTION_CURSOR {
        INTEGER unit_id PK, FK
        INTEGER next_nsu
        INTEGER last_processed_nsu
        INTEGER last_http_status
        INTEGER consecutive_errors
        TEXT last_error
        TEXT last_success_at
        INTEGER history_target_nsu
        TEXT history_backfilled_at
        TEXT next_poll_at
        TEXT updated_at
    }

    COLLECTION_JOB {
        TEXT id PK "UUID publico da API"
        TEXT trigger_source
        TEXT requested_unit_code
        TEXT requested_by
        TEXT status
        TEXT created_at
        TEXT started_at
        TEXT finished_at
        INTEGER received_documents
        INTEGER stored_documents
        INTEGER ignored_documents
        INTEGER error_count
    }

    COLLECTION_RUN {
        INTEGER id PK
        TEXT job_id FK
        INTEGER unit_id FK
        TEXT started_at
        TEXT finished_at
        TEXT result
        INTEGER requested_batches
        INTEGER received_documents
        INTEGER stored_documents
        INTEGER ignored_documents
        TEXT error_message
    }

    COLLECTION_EVENT {
        INTEGER id PK
        INTEGER run_id FK
        INTEGER unit_id FK
        INTEGER nsu
        TEXT access_key
        TEXT event_type
        TEXT message
        TEXT created_at
    }

    DFE_ARTIFACT {
        INTEGER id PK
        INTEGER unit_id FK
        INTEGER nsu
        TEXT access_key
        TEXT schema_name
        TEXT document_type
        TEXT generated_at
        BLOB xml_gzip
        BLOB xml_content
        TEXT xml_sha256
        TEXT received_at
    }

    INVOICE {
        INTEGER id PK
        INTEGER unit_id FK
        INTEGER source_artifact_id FK
        TEXT access_key
        TEXT document_number
        TEXT series
        TEXT issued_at
        TEXT competence_date
        TEXT provider_tax_id
        TEXT provider_name
        TEXT taker_tax_id
        TEXT taker_name
        TEXT service_code
        TEXT service_description
        INTEGER service_amount_cents
        INTEGER net_amount_cents
        TEXT fiscal_status
        INTEGER contract_id "referencia logica PHP"
        TEXT contract_number
        INTEGER version
        TEXT created_at
        TEXT updated_at
    }

    INVOICE_ITEM {
        INTEGER id PK
        INTEGER invoice_id FK
        INTEGER item_number
        TEXT code
        TEXT description
        TEXT quantity
        INTEGER total_amount_cents
    }

    FISCAL_EVENT {
        INTEGER id PK
        INTEGER unit_id FK
        INTEGER source_artifact_id FK
        TEXT invoice_access_key
        TEXT event_key
        TEXT event_type
        INTEGER event_sequence
        TEXT occurred_at
        TEXT protocol
        TEXT status
        TEXT created_at
    }

    INTEGRATION_OUTBOX {
        INTEGER id PK
        TEXT aggregate_type
        INTEGER aggregate_id "referencia polimorfica"
        TEXT operation
        INTEGER aggregate_version
        TEXT created_at
    }

    SYNC_RUN {
        TEXT id PK
        TEXT status
        TEXT trigger_source
        TEXT created_at
        TEXT started_at
        TEXT finished_at
        TEXT local_path
        TEXT remote_path
        INTEGER size_bytes
        TEXT sha256
        INTEGER attempts
        TEXT error_message
    }

    FISCAL_UNIT ||--|| DIGITAL_CERTIFICATE : "usa certificado ativo"
    FISCAL_UNIT ||--|| DISTRIBUTION_CURSOR : "possui cursor"
    FISCAL_UNIT ||--o{ COLLECTION_RUN : "executa coletas"
    COLLECTION_JOB ||--o{ COLLECTION_RUN : "agrupa unidades"
    COLLECTION_RUN ||--o{ COLLECTION_EVENT : "registra ocorrencias"
    FISCAL_UNIT ||--o{ COLLECTION_EVENT : "gera ocorrencias"
    FISCAL_UNIT ||--o{ DFE_ARTIFACT : "recebe DF-e"
    FISCAL_UNIT ||--o{ INVOICE : "recebe NFS-e"
    FISCAL_UNIT ||--o{ FISCAL_EVENT : "recebe eventos"
    DFE_ARTIFACT ||--o| INVOICE : "origina nota"
    DFE_ARTIFACT ||--o{ FISCAL_EVENT : "origina evento"
    INVOICE ||--o{ INVOICE_ITEM : "contem itens"
    INVOICE ||--o{ INTEGRATION_OUTBOX : "publica versoes"
```

## 2. Responsabilidade das entidades

| Entidade | Responsabilidade |
|---|---|
| `schema_version` | Controla as migracoes aplicadas ao SQLite. |
| `fiscal_unit` | Unidade consultada no ADN, seu CNPJ, ambiente e referencia do certificado. Nao guarda senha ou chave privada. |
| `digital_certificate` | Caminhos e metadados do A1 por unidade. Extrai thumbprint e validade do PFX/PEM; guarda apenas o nome da variavel da senha. `store_location` e nulo para arquivos no Ubuntu e existe somente para compatibilidade Windows legada. |
| `distribution_cursor` | Estado operacional por unidade: proximo NSU, tentativas, agendamento e progresso do backfill historico. |
| `collection_job` | Fila persistente e estado publico de uma solicitacao da API, scheduler ou CLI. |
| `collection_run` | Auditoria de cada ciclo, com lotes solicitados, documentos recebidos/salvos e eventual erro. |
| `collection_event` | Log estruturado de duplicidades e demais ocorrencias da coleta. |
| `dfe_artifact` | Evidencia fiscal original. Preserva XML comprimido, XML consultavel e SHA-256. |
| `invoice` | Cabecalho normalizado da NFS-e e futura referencia ao contrato PHP. |
| `invoice_item` | Itens/servicos extraidos do XML. |
| `fiscal_event` | Eventos fiscais associados pela chave da NFS-e, incluindo o codigo nacional do evento (`event_code`). |
| `integration_outbox` | Fila imutavel de alteracoes para consumo idempotente pelo PHP. |
| `sync_run` | Auditoria da copia consistente e transferencia atomica do espelho por SFTP. |

## 3. Chaves, unicidade e integridade

| Tabela | Regra |
|---|---|
| `fiscal_unit` | `code` e unico; `(tax_id, environment)` tambem e unico. |
| `distribution_cursor` | Relacao 1:1 com `fiscal_unit` por `unit_id`. |
| `dfe_artifact` | `(unit_id, nsu)` e unico. O mesmo NSU com hash XML diferente interrompe o avanço do cursor. |
| `invoice` | `(unit_id, access_key)` e unico. Valores monetarios sao armazenados em centavos. |
| `invoice_item` | `(invoice_id, item_number)` e unico; exclusao da nota remove os itens em cascata. |
| `fiscal_event` | `(unit_id, event_key)` e unico. |
| `integration_outbox` | `(aggregate_type, aggregate_id, operation, aggregate_version)` e unico. |

Indices de consulta:

- artefato por `(unit_id, access_key)`;
- nota por `(unit_id, provider_tax_id)`;
- nota por `(unit_id, taker_tax_id)`;
- nota por `(unit_id, issued_at)`.

## 4. Backfill e cursor NSU

```mermaid
stateDiagram-v2
    [*] --> Corrente: next_nsu
    Corrente --> Backfill: Buscar periodo
    Backfill --> Backfill: atualiza next_nsu
    Backfill --> Interrompido: processo encerrado
    Interrompido --> Backfill: retoma com history_target_nsu
    Backfill --> Completo: next_nsu >= history_target_nsu
    Completo --> Corrente: grava history_backfilled_at
```

- `history_target_nsu` preserva o cursor que existia antes do retorno ao NSU 1.
- Se houver interrupcao, o alvo permanece gravado para retomada segura.
- `history_backfilled_at` somente e preenchido quando o alvo e alcancado.
- Alterar datas no monitor nao muda diretamente o protocolo do ADN; as datas filtram as notas depois da varredura por NSU.

## 5. Views de consumo

### `vw_notas_fiscais`

Visao operacional solicitada para consulta humana e integracao simples:

| Campo | Origem |
|---|---|
| `Contrato` | `invoice.contract_number` |
| `ID` | `invoice.id` |
| `Contrato ID` | `invoice.contract_id` |
| `Unidade CNPJ` | `fiscal_unit.tax_id` |
| `Fornecedor CNPJ` | `invoice.provider_tax_id` |
| `Data de Emissao` | `invoice.issued_at` |
| `Valor` | `invoice.service_amount_cents / 100` |
| `Competencia` | `invoice.competence_date` |
| `XML` | `dfe_artifact.xml_content` |
| `Chave de Acesso` | `invoice.access_key` |
| `NSU` | `dfe_artifact.nsu` |

### `vw_invoice_outbox`

Contrato de leitura para o sistema PHP. Combina outbox, unidade, nota e artefato sem expor o BLOB do XML na listagem principal.

## 6. MER logico de integracao com o sistema PHP

Os relacionamentos abaixo atravessam bancos diferentes. Portanto sao **referencias logicas**, nao chaves estrangeiras fisicas do SQLite.

```mermaid
erDiagram
    SYSTEM_UNIT {
        INTEGER id PK
        TEXT name
    }

    UNIDADE {
        INTEGER id PK
        INTEGER system_unit_id FK
        INTEGER pessoa_id FK
    }

    PESSOA {
        INTEGER id PK
        TEXT numero "CPF ou CNPJ"
        TEXT nome
    }

    FORNECEDOR {
        INTEGER id PK
        INTEGER pessoa_id FK
        INTEGER system_unit_id FK
        TEXT nome
    }

    SERVICO {
        INTEGER id PK
        TEXT servico
    }

    CONTRATO {
        INTEGER id PK
        INTEGER system_unit_id FK
        INTEGER fornecedor_id FK
        INTEGER servico_id FK
        TEXT numero
        DATE dt_inicio
        DATE dt_fim
        DATE dt_rescisao
    }

    NOTA_FISCAL_PHP {
        INTEGER id PK
        INTEGER fornecedor_id FK
        INTEGER contrato_id FK
        INTEGER system_unit_id FK
        INTEGER servico_id FK
        TEXT chave_acesso
        INTEGER nsu
        TEXT cnpj_emitente
        TEXT cnpj_destinatario
        DATE dt_emissao
        DATE dt_competencia
        DECIMAL valor
        TEXT arquivo_xml
    }

    INVOICE_SQLITE {
        INTEGER id PK
        INTEGER contract_id "referencia logica"
        TEXT contract_number
        TEXT access_key UK
        TEXT provider_tax_id
        TEXT taker_tax_id
        TEXT issued_at
    }

    SYSTEM_UNIT ||--o{ UNIDADE : "organiza"
    PESSOA ||--o| UNIDADE : "identifica CNPJ"
    PESSOA ||--o| FORNECEDOR : "identifica CNPJ"
    SYSTEM_UNIT ||--o{ FORNECEDOR : "cadastra"
    SYSTEM_UNIT ||--o{ CONTRATO : "possui"
    FORNECEDOR ||--o{ CONTRATO : "celebra"
    SERVICO ||--o{ CONTRATO : "classifica"
    CONTRATO ||--o{ NOTA_FISCAL_PHP : "recebe"
    FORNECEDOR ||--o{ NOTA_FISCAL_PHP : "emite"
    INVOICE_SQLITE |o--o| NOTA_FISCAL_PHP : "sincronizada por chave"
    INVOICE_SQLITE }o--o| CONTRATO : "conciliada"
```

## 7. Regras propostas para conciliacao contratual

Uma NFS-e somente deve receber `contract_id` quando houver uma correspondencia unica:

1. `invoice.taker_tax_id` corresponde ao CNPJ da unidade;
2. `invoice.provider_tax_id` corresponde ao CNPJ de `pessoa.numero` do fornecedor;
3. `invoice.issued_at` esta dentro de `contrato.dt_inicio` e `contrato.dt_fim`;
4. contrato nao esta excluido nem rescindido na data de emissao;
5. existe apenas um contrato elegivel para unidade, fornecedor e periodo.

Se houver zero ou mais de um contrato elegivel, a NFS-e permanece sem vinculo automatico e deve entrar em uma fila de conciliacao manual. A chave de acesso e a chave natural para sincronizacao idempotente com `nota_fiscal.chave_acesso`.

## 8. Fluxo de dados

```mermaid
flowchart LR
    ADN["ADN NFS-e"] -->|"DFe por NSU"| ART["dfe_artifact"]
    ART -->|"parse XML"| INV["invoice / invoice_item"]
    INV --> OUT["integration_outbox"]
    OUT --> PHP["Sistema PHP de contratos"]
    PHP -->|"contrato unico"| LINK["contract_id / contract_number"]
    LINK --> INV
    INV --> VIEW["vw_notas_fiscais"]
```

## 9. Observacoes de seguranca e auditoria

- A chave privada do A1 nao e armazenada no SQLite; fica no repositorio de certificados do Windows.
- XML e PDF sao preservados como BLOB, acompanhados de SHA-256.
- A outbox registra cada versao da nota para integracao idempotente.
- `collection_run` e os campos de erro do cursor permitem auditoria operacional.
- `contract_id` no SQLite nao possui FK fisica porque o contrato esta no MySQL do PHP.
