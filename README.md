# coletor-empenhos

Baixa o arquivo anual de despesa do Portal da Transparência do Distrito Federal,
que traz as notas de empenho do GDF, e o salva exatamente como foi recebido. O
conteúdo não é descompactado, filtrado nem reescrito: tratamento, recortes (como
o dos medicamentos) e cruzamentos ficam no repositório `medicamentos-fdcc`.

Criado a partir do [python-template-tb](https://github.com/Transparencia-Brasil/python-template-tb).

## Fonte e abrangência

- Portal: <https://www.transparencia.df.gov.br/#/dados-abertos>.
- Arquivo baixado: `Despesa_{ano}.zip`, em
  `https://www.transparencia.df.gov.br/arquivos/portal-dados-abertos/`.

Pontos verificados na fonte em 15/11/2026:

- **Só há arquivos anuais**, de 2009 a 2026. O portal não publica arquivos mensais
  da despesa; um ano não publicado responde HTTP 404.
- **O arquivo cobre só o GDF.** Órgãos federais, inclusive os sediados no DF, não
  aparecem. A Secretaria de Saúde é a UG 170101.
- **O ZIP de 2025 tem 55 MB** (cerca de 600 MB descompactado) e traz 11 CSVs numa
  pasta `Despesa_{ano}/`: `Despesa_Empenho`, `Despesa_Empenho_Subelemento`,
  `Despesa_Empenho_Descricao` (itens de cada empenho), liquidações, pagamentos e
  ordens bancárias. O de 2025 tem 112.748 notas de empenho, emitidas até 31/12/2025.
- **Os anos são republicados.** O ZIP de 2025 tinha `Last-Modified` de 15/10/2026,
  e o de 2026, de 29/10/2026. A data de cada arquivo aparece no log.
- **Os CSVs não seguem o padrão comum:** usam Latin-1, `¨` como separador, todos os
  campos entre aspas e aspas sem escape dentro do texto. O `pandas.read_csv` não os
  lê direto; quem tratar os dados precisa separar as linhas por `"¨"`.

A consulta por mês só existe na API do portal, que é paginada e não faz parte deste
coletor.

## Instalação

Requer Python **3.10 ou superior**. Na raiz do repositório:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

No Linux/macOS, ative o ambiente com `source .venv/bin/activate`. Use `python3`
no lugar de `python` se necessário.

## Execução

```powershell
python src/coletores/coleta_empenhos.py --ano 2025
```

`--ano` é obrigatório e tem quatro dígitos. Se o ano foi publicado, quem decide é
a fonte.

Para executar pelo Bash, de qualquer diretório, use os mesmos argumentos:

```bash
bash src/coletores/run-coleta-empenhos.sh --ano 2025
```

O wrapper usa o Python do `.venv` do repositório quando ele existe; caso
contrário, usa `python3` ou `python` do sistema. Funciona no Git Bash e no WSL.

## Saída

Cada download gera um arquivo novo em `outputs/`, na raiz do repositório,
qualquer que seja o diretório de execução, numa subpasta por ano. A pasta é
ignorada pelo Git.

```text
outputs/{ano}/{AAAAMMDD}_{HHMMSS}_despesa_{ano}.zip

outputs/2025/20261115_212234_despesa_2025.zip
```

A data e a hora são as do início do download, no horário local da máquina, e
indicam quando o arquivo foi baixado, não a versão dos dados. Downloads
anteriores nunca são substituídos: as cópias se acumulam até serem removidas
manualmente.

### Escolher o arquivo de saída

O parâmetro opcional `--saida` substitui o caminho automático pelo caminho
completo do ZIP, o que permite a outro repositório decidir onde o arquivo fica:

```bash
bash /caminho/para/coletor-empenhos/src/coletores/run-coleta-empenhos.sh \
  --ano 2025 --saida tasks/coleta-empenhos/outputs/2025/despesa_2025.zip
```

Um caminho relativo é resolvido a partir do diretório de onde o comando é
executado. Se o caminho já existir, a coleta é recusada com código 2, sem acessar
a fonte.

O download é feito em streaming para um arquivo `.part` na mesma pasta. O nome
final só é usado depois que o arquivo chega inteiro, o total recebido confere com
o `Content-Length` e o conteúdo é reconhecido como ZIP. Em caso de falha, o
`.part` é removido. O arquivo é pedido sem compressão (`Accept-Encoding:
identity`), para que o conteúdo salvo seja exatamente o publicado.

## Códigos de saída

| Código | Situação |
| --- | --- |
| **0** | Arquivo salvo. |
| **1** | Falha de rede, HTTP diferente de 200 e 404, download incompleto, conteúdo que não é ZIP ou erro de escrita. |
| **2** | Argumento ou configuração inválidos, ano não publicado (404) ou destino já existente. |

## Configuração

As variáveis podem ser definidas no ambiente ou num `.env` na raiz do
repositório, usando `.env.example` como referência. O ambiente tem precedência.

| Variável | Padrão | Descrição |
| --- | --- | --- |
| `PORTAL_DADOS_ABERTOS_URL` | `https://www.transparencia.df.gov.br/arquivos/portal-dados-abertos` | Pasta dos arquivos de dados abertos, HTTP(S), sem query ou fragmento. |
| `DOWNLOAD_TIMEOUT_SECONDS` | `60` | Timeout positivo, por tentativa, para conectar e entre leituras da resposta. |

Falhas de conexão e respostas HTTP **429, 500, 502, 503 e 504** são repetidas
até oito vezes, com espera crescente de fator 2 e respeito ao cabeçalho
`Retry-After`.

## Testes

```powershell
python -m unittest discover -s tests -v
```

Os testes usam um servidor HTTP local e não acessam o portal. Cobrem a
preservação byte a byte do ZIP, a URL e o destino, ausência de `Content-Length`,
404 (sem deixar pasta vazia) e outros status, download truncado, conteúdo que não
é ZIP, retentativa após 503, validação do ano e da configuração, `--saida`
absoluto, relativo e já ocupado, e o wrapper Bash chamado de outro diretório.
