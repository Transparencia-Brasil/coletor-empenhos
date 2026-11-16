r"""Baixa o arquivo anual de despesa do Portal da Transparência do DF.

Uso, a partir de qualquer diretório:
    python src/coletores/coleta_empenhos.py --ano 2025
    python src/coletores/coleta_empenhos.py --ano 2025 --saida caminho/despesa_2025.zip

O arquivo Despesa_{ano}.zip traz as notas de empenho do GDF e as tabelas ligadas
a elas (subelemento, descrição dos itens, liquidações e pagamentos). Ele é salvo
byte a byte como foi recebido, em outputs/{ano}/, com a data e a hora do download
no nome. Nada é descompactado, filtrado ou reescrito.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import re
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

RAIZ_REPO = Path(__file__).resolve().parents[2]
PASTA_SAIDA = RAIZ_REPO / "outputs"
BASE_PADRAO = "https://www.transparencia.df.gov.br/arquivos/portal-dados-abertos"
TIMEOUT_PADRAO = "60"
TAMANHO_BLOCO = 1024 * 1024
# [0-9] em vez de \d, que também aceitaria dígitos Unicode como "２０２５".
PADRAO_ANO = re.compile(r"[0-9]{4}")

log = logging.getLogger("coleta_empenhos")


@dataclass(frozen=True)
class Configuracao:
    """Parâmetros de acesso ao portal.

    Attributes:
        url_base: URL da pasta de dados abertos, sem barra final.
        timeout: Segundos de espera, por tentativa, para conectar e entre
            leituras sucessivas da resposta.
    """

    url_base: str
    timeout: float


@dataclass(frozen=True)
class Download:
    """Resumo de um download concluído.

    Attributes:
        bytes_gravados: Tamanho do arquivo salvo.
        last_modified: Cabeçalho Last-Modified da fonte, se enviado.
        content_length: Tamanho declarado pela fonte, se enviado.
    """

    bytes_gravados: int
    last_modified: str | None
    content_length: int | None


class AnoNaoPublicado(Exception):
    """O portal respondeu 404 para o arquivo do ano pedido."""


class DownloadIncompleto(Exception):
    """O total recebido difere do Content-Length declarado pela fonte."""


class ArquivoInvalido(Exception):
    """O conteúdo recebido não é um ZIP, como uma página de erro servida com 200."""


def configuracao() -> Configuracao:
    """Lê a configuração do ambiente e do .env da raiz do repositório.

    Variáveis já definidas no ambiente têm precedência sobre o .env.

    Raises:
        ValueError: Se a URL base não for HTTP(S) com host, ou tiver query ou
            fragmento, ou se o timeout não for um número positivo e finito.
    """
    load_dotenv(RAIZ_REPO / ".env", override=False)
    url_base = os.getenv("PORTAL_DADOS_ABERTOS_URL", BASE_PADRAO).rstrip("/")
    url = urlsplit(url_base)
    if url.scheme not in ("https", "http") or not url.netloc or url.query or url.fragment:
        raise ValueError("PORTAL_DADOS_ABERTOS_URL deve ser uma URL HTTP(S) sem query ou fragmento")
    try:
        timeout = float(os.getenv("DOWNLOAD_TIMEOUT_SECONDS", TIMEOUT_PADRAO))
    except ValueError as erro:
        raise ValueError("DOWNLOAD_TIMEOUT_SECONDS deve ser um número") from erro
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("DOWNLOAD_TIMEOUT_SECONDS deve ser positivo e finito")
    return Configuracao(url_base, timeout)


def valida_ano(texto: str) -> str:
    """Aceita somente quatro dígitos ASCII; usado como type do argparse.

    Se o ano foi publicado, quem decide é o portal, que responde 404 quando não.
    """
    if not PADRAO_ANO.fullmatch(texto):
        raise argparse.ArgumentTypeError(f"ano inválido: {texto!r}; use quatro dígitos, como 2025")
    return texto


def url_arquivo(config: Configuracao, ano: str) -> str:
    """Monta a URL do arquivo anual, como {url_base}/Despesa_2025.zip."""
    return f"{config.url_base}/Despesa_{ano}.zip"


def destino_arquivo(ano: str, momento: datetime) -> Path:
    """Monta o caminho local de um download, como outputs/2025/20261115_102915_despesa_2025.zip.

    O nome começa pela data e hora do download (horário local), para que a ordem
    alfabética seja a cronológica.
    """
    return PASTA_SAIDA / ano / f"{momento:%Y%m%d_%H%M%S}_despesa_{ano}.zip"


def sessao_http() -> requests.Session:
    """Cria uma sessão que repete GETs com falhas transitórias.

    Repete até 8 vezes falhas de conexão e respostas 429, 500, 502, 503 e 504,
    com espera crescente de fator 2 e respeito ao cabeçalho Retry-After. Esgotadas
    as tentativas, a última resposta é devolvida para quem chamou tratar o status.
    """
    tentativas = Retry(
        total=8,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    sessao = requests.Session()
    adaptador = HTTPAdapter(max_retries=tentativas)
    sessao.mount("https://", adaptador)
    sessao.mount("http://", adaptador)
    sessao.headers["User-Agent"] = "Transparencia-Brasil/coletor-empenhos"
    # Sem compressão de transporte: o arquivo salvo tem de ser o publicado.
    sessao.headers["Accept-Encoding"] = "identity"
    return sessao


def baixa_arquivo(sessao: requests.Session, url: str, destino: Path, timeout: float) -> Download:
    """Baixa a URL em streaming e publica o arquivo completo de forma atômica.

    O corpo é gravado num arquivo .part ao lado do destino e só recebe o nome
    final depois de chegar inteiro e de ser reconhecido como ZIP. Em qualquer
    falha ou interrupção, o .part é removido.

    Raises:
        AnoNaoPublicado: Se o portal responder 404.
        requests.HTTPError: Para outro status diferente de 200.
        DownloadIncompleto: Se o total gravado diferir do Content-Length.
        ArquivoInvalido: Se o conteúdo recebido não for um ZIP.
        requests.RequestException: Para falhas de conexão ou de leitura.
        OSError: Para falhas ao gravar o arquivo.
    """
    part = destino.with_name(destino.name + ".part")
    try:
        with sessao.get(url, stream=True, timeout=(timeout, timeout)) as resposta:
            if resposta.status_code == 404:
                raise AnoNaoPublicado(url)
            if resposta.status_code != 200:
                raise requests.HTTPError(f"HTTP {resposta.status_code} em {url}", response=resposta)
            # A pasta só é criada depois da resposta 200, para um 404 não deixar pasta vazia.
            destino.parent.mkdir(parents=True, exist_ok=True)
            cabecalho_tamanho = resposta.headers.get("Content-Length")
            content_length = int(cabecalho_tamanho) if cabecalho_tamanho is not None else None
            gravados = 0
            with part.open("wb") as saida:
                for bloco in resposta.iter_content(TAMANHO_BLOCO):
                    saida.write(bloco)
                    gravados += len(bloco)
            if content_length is not None and gravados != content_length:
                raise DownloadIncompleto(
                    f"recebidos {gravados} de {content_length} bytes declarados em {url}"
                )
            last_modified = resposta.headers.get("Last-Modified")
        if not zipfile.is_zipfile(part):
            raise ArquivoInvalido(f"o conteúdo recebido de {url} não é um ZIP")
        part.replace(destino)
    finally:
        part.unlink(missing_ok=True)
    return Download(gravados, last_modified, content_length)


def le_argumentos(argv: list[str] | None = None) -> tuple[str, Path | None]:
    """Lê --ano e --saida; argumentos inválidos encerram com código 2 pelo argparse."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--ano", required=True, type=valida_ano,
                        help="Ano com quatro dígitos (ex.: 2025).")
    parser.add_argument(
        "--saida", type=Path,
        help="Caminho completo do ZIP a gravar, relativo ao diretório de execução se não for "
             "absoluto; sem ele, o arquivo vai para outputs/{ano}/ com nome automático.",
    )
    args = parser.parse_args(argv)
    return args.ano, args.saida


def main(argv: list[str] | None = None) -> int:
    """Executa a coleta do ano pedido e devolve o código de saída.

    Returns:
        0 quando o arquivo é salvo; 1 para falha de rede, HTTP diferente de 200 e
        404, download incompleto, conteúdo que não é ZIP ou erro de escrita; 2 para
        configuração inválida, ano não publicado ou destino já existente.
    """
    ano, saida = le_argumentos(argv)
    try:
        config = configuracao()
    except ValueError as erro:
        log.error("Configuração inválida: %s", erro)
        return 2

    url = url_arquivo(config, ano)
    destino = saida.resolve() if saida is not None else destino_arquivo(ano, datetime.now())
    if destino.exists():
        log.error("%s já existe; nada foi baixado", destino)
        return 2

    log.info("Baixando %s", url)
    inicio = time.monotonic()
    try:
        with sessao_http() as sessao:
            download = baixa_arquivo(sessao, url, destino, config.timeout)
    except AnoNaoPublicado:
        log.error("Ano %s não publicado no portal (%s)", ano, url)
        return 2
    except (requests.RequestException, DownloadIncompleto, ArquivoInvalido, OSError) as erro:
        log.error("Falha no download: %s", erro)
        return 1

    log.info(
        "Arquivo salvo em %s: %d bytes (Content-Length: %s), Last-Modified: %s, %.1f s",
        destino,
        download.bytes_gravados,
        download.content_length if download.content_length is not None else "tamanho não declarado",
        download.last_modified or "não informado",
        time.monotonic() - inicio,
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    sys.exit(main())
