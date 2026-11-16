"""Testes do coletor de empenhos, sem acesso ao portal público."""

import io
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import requests

from src.coletores import coleta_empenhos as coletor

RAIZ = Path(__file__).resolve().parents[1]
LAST_MODIFIED = "Thu, 15 Oct 2026 13:51:54 GMT"
MOMENTO = datetime(2026, 11, 15, 10, 29, 15)


def zip_exemplo():
    """ZIP pequeno com a mesma estrutura do publicado: uma pasta com CSVs em Latin-1."""
    memoria = io.BytesIO()
    with zipfile.ZipFile(memoria, "w", zipfile.ZIP_DEFLATED) as arquivo:
        arquivo.writestr(
            "Despesa_2025/Despesa_Empenho_2025.csv",
            '"NOTA EMPENHO"¨"EXERCÍCIO"\r\n"2025NE00001"¨"2025"\r\n'.encode("latin-1"),
        )
    return memoria.getvalue()


CORPO = zip_exemplo()


def resposta(status=200, corpo=CORPO, cabecalhos=None, content_length=None):
    """Descreve uma resposta do servidor local.

    content_length=None declara o tamanho real do corpo; False omite o
    cabeçalho; um inteiro declara esse valor, mesmo que diferente do corpo.
    """
    return {"status": status, "corpo": corpo, "cabecalhos": cabecalhos or {},
            "content_length": content_length}


class ServidorLocal:
    """Servidor HTTP em thread que responde, em ordem, a respostas enfileiradas.

    Depois que a fila acaba, a última resposta é repetida. Usa HTTP/1.0, então
    cada resposta fecha a conexão, o que permite corpos menores que o tamanho
    declarado.
    """

    def __init__(self):
        self.respostas = [resposta()]
        self.caminhos = []
        self.cabecalhos_recebidos = []
        servidor = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                servidor.caminhos.append(self.path)
                servidor.cabecalhos_recebidos.append(dict(self.headers))
                atual = servidor.respostas.pop(0) if len(servidor.respostas) > 1 else servidor.respostas[0]
                self.send_response(atual["status"])
                for nome, valor in atual["cabecalhos"].items():
                    self.send_header(nome, valor)
                if atual["content_length"] is None:
                    self.send_header("Content-Length", str(len(atual["corpo"])))
                elif atual["content_length"] is not False:
                    self.send_header("Content-Length", str(atual["content_length"]))
                self.end_headers()
                self.wfile.write(atual["corpo"])

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}/base"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def fecha(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()


class ComServidor(unittest.TestCase):
    """Base para testes que usam o servidor local e uma pasta temporária."""

    def setUp(self):
        self.servidor = ServidorLocal()
        self.addCleanup(self.servidor.fecha)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.pasta = Path(tmp.name)
        self.destino = self.pasta / "Despesa_2025.zip"
        self.part = self.destino.with_name(self.destino.name + ".part")
        self.url = coletor.url_arquivo(coletor.Configuracao(self.servidor.base, 5.0), "2025")


class TestConfiguracao(unittest.TestCase):
    def configuracao_com(self, valores):
        """Lê a configuração com o ambiente isolado e sem ler o .env local."""
        with patch.dict(os.environ, valores, clear=True), patch.object(coletor, "load_dotenv"):
            return coletor.configuracao()

    def test_url_do_arquivo_anual(self):
        config = coletor.Configuracao("https://exemplo.df.gov.br/base", 60.0)
        self.assertEqual(coletor.url_arquivo(config, "2025"),
                         "https://exemplo.df.gov.br/base/Despesa_2025.zip")

    def test_padroes(self):
        config = self.configuracao_com({})
        self.assertEqual(config.url_base,
                         "https://www.transparencia.df.gov.br/arquivos/portal-dados-abertos")
        self.assertEqual(config.timeout, 60.0)

    def test_ambiente_tem_precedencia_e_barra_final_removida(self):
        config = self.configuracao_com({"PORTAL_DADOS_ABERTOS_URL": "http://127.0.0.1:8000/base/",
                                        "DOWNLOAD_TIMEOUT_SECONDS": "5"})
        self.assertEqual(config.url_base, "http://127.0.0.1:8000/base")
        self.assertEqual(config.timeout, 5.0)

    def test_configuracao_invalida(self):
        for valores in (
            {"PORTAL_DADOS_ABERTOS_URL": "ftp://exemplo.df.gov.br/base"},
            {"PORTAL_DADOS_ABERTOS_URL": "https://exemplo.df.gov.br/base?a=1"},
            {"DOWNLOAD_TIMEOUT_SECONDS": "abc"},
            {"DOWNLOAD_TIMEOUT_SECONDS": "0"},
            {"DOWNLOAD_TIMEOUT_SECONDS": "inf"},
        ):
            with self.subTest(valores=valores), self.assertRaises(ValueError):
                self.configuracao_com(valores)

    def test_destino_com_data_e_hora_do_download(self):
        self.assertEqual(
            coletor.destino_arquivo("2025", MOMENTO),
            coletor.PASTA_SAIDA / "2025" / "20261115_102915_despesa_2025.zip",
        )


class TestArgumentos(unittest.TestCase):
    def test_ano_e_saida(self):
        self.assertEqual(coletor.le_argumentos(["--ano", "2025"]), ("2025", None))
        self.assertEqual(coletor.le_argumentos(["--ano", "2025", "--saida", "x.zip"]),
                         ("2025", Path("x.zip")))

    def test_ano_invalido_ou_ausente(self):
        for argv in (["--ano", "25"], ["--ano", "２０２５"], ["--ano", "2025a"], [],
                     ["--ano", "2025", "--mes", "07"]):
            with self.subTest(argv=argv), patch("sys.stderr"), self.assertRaises(SystemExit) as erro:
                coletor.le_argumentos(argv)
            self.assertEqual(erro.exception.code, 2)


class TestDownload(ComServidor):
    def baixa(self):
        with coletor.sessao_http() as sessao:
            return coletor.baixa_arquivo(sessao, self.url, self.destino, 5.0)

    def test_conteudo_identico_byte_a_byte(self):
        self.servidor.respostas = [resposta(cabecalhos={"Last-Modified": LAST_MODIFIED})]
        download = self.baixa()
        self.assertEqual(self.destino.read_bytes(), CORPO)
        self.assertEqual(download.bytes_gravados, len(CORPO))
        self.assertEqual(download.content_length, len(CORPO))
        self.assertEqual(download.last_modified, LAST_MODIFIED)
        self.assertEqual(self.servidor.caminhos, ["/base/Despesa_2025.zip"])
        self.assertFalse(self.part.exists())

    def test_pede_conteudo_sem_compressao(self):
        self.baixa()
        self.assertEqual(self.servidor.cabecalhos_recebidos[0].get("Accept-Encoding"), "identity")

    def test_corpo_maior_que_um_bloco(self):
        with patch.object(coletor, "TAMANHO_BLOCO", 7):
            self.baixa()
        self.assertEqual(self.destino.read_bytes(), CORPO)

    def test_sem_content_length(self):
        self.servidor.respostas = [resposta(content_length=False)]
        download = self.baixa()
        self.assertEqual(self.destino.read_bytes(), CORPO)
        self.assertIsNone(download.content_length)

    def test_404(self):
        self.servidor.respostas = [resposta(status=404, corpo=b"")]
        with self.assertRaises(coletor.AnoNaoPublicado):
            self.baixa()
        self.assertFalse(self.destino.exists())

    def test_status_nao_200(self):
        self.servidor.respostas = [resposta(status=403, corpo=b"")]
        with self.assertRaises(requests.HTTPError):
            self.baixa()
        self.assertFalse(self.destino.exists())

    def test_truncado(self):
        self.servidor.respostas = [resposta(content_length=len(CORPO) + 100)]
        with self.assertRaises((coletor.DownloadIncompleto, requests.RequestException)):
            self.baixa()
        self.assertFalse(self.destino.exists())
        self.assertFalse(self.part.exists())

    def test_conteudo_que_nao_e_zip(self):
        # Uma página de erro servida com 200 não pode virar um .zip na pasta de saída.
        self.servidor.respostas = [resposta(corpo=b"<html>erro</html>")]
        with self.assertRaises(coletor.ArquivoInvalido):
            self.baixa()
        self.assertFalse(self.destino.exists())
        self.assertFalse(self.part.exists())

    def test_retentativa_503(self):
        self.servidor.respostas = [resposta(status=503, corpo=b""), resposta()]
        self.baixa()  # a primeira retentativa do urllib3 não espera
        self.assertEqual(self.destino.read_bytes(), CORPO)
        self.assertEqual(len(self.servidor.caminhos), 2)


class TestMain(ComServidor):
    def setUp(self):
        super().setUp()
        for contexto in (
            patch.dict(os.environ, {"PORTAL_DADOS_ABERTOS_URL": self.servidor.base,
                                    "DOWNLOAD_TIMEOUT_SECONDS": "5"}),
            patch.object(coletor, "PASTA_SAIDA", self.pasta),
            patch.object(coletor, "datetime", wraps=datetime),
        ):
            contexto.start()
            self.addCleanup(contexto.stop)
        coletor.datetime.now.return_value = MOMENTO
        # Caminho esperado escrito por extenso, sem usar destino_arquivo().
        self.destino = self.pasta / "2025" / "20261115_102915_despesa_2025.zip"

    def executa(self, *argumentos):
        """Roda main() e devolve o código de saída e as mensagens registradas.

        Sem argumentos, pede o ano de 2025.
        """
        with self.assertLogs("coleta_empenhos", level="INFO") as log:
            codigo = coletor.main(list(argumentos) or ["--ano", "2025"])
        return codigo, "\n".join(log.output)

    def test_main_sucesso(self):
        self.servidor.respostas = [resposta(cabecalhos={"Last-Modified": LAST_MODIFIED})]
        codigo, log = self.executa()
        self.assertEqual(codigo, 0)
        self.assertEqual(self.destino.read_bytes(), CORPO)
        self.assertEqual(self.servidor.caminhos, ["/base/Despesa_2025.zip"])
        self.assertIn(LAST_MODIFIED, log)
        self.assertIn(str(len(CORPO)), log)
        self.assertIn(str(self.destino), log)

    def test_main_nome_existente_recusado(self):
        self.destino.parent.mkdir(parents=True)
        self.destino.write_bytes(b"antigo")
        codigo, log = self.executa()
        self.assertEqual(codigo, 2)
        self.assertEqual(self.destino.read_bytes(), b"antigo")
        self.assertEqual(self.servidor.caminhos, [])
        self.assertIn(str(self.destino), log)

    def test_main_saida_absoluta_substitui_destino_automatico(self):
        saida = self.pasta / "outro" / "despesa.zip"
        codigo, log = self.executa("--ano", "2025", "--saida", str(saida))
        self.assertEqual(codigo, 0)
        self.assertEqual(saida.read_bytes(), CORPO)
        self.assertFalse((self.pasta / "2025").exists())
        self.assertIn(str(saida), log)

    def test_main_saida_relativa_ao_diretorio_de_execucao(self):
        with tempfile.TemporaryDirectory() as tmp:
            diretorio_anterior = os.getcwd()
            os.chdir(tmp)
            try:
                codigo, _ = self.executa("--ano", "2025", "--saida", "dados/despesa.zip")
            finally:
                os.chdir(diretorio_anterior)
            self.assertEqual(codigo, 0)
            self.assertEqual((Path(tmp) / "dados" / "despesa.zip").read_bytes(), CORPO)

    def test_main_saida_existente_recusada(self):
        saida = self.pasta / "despesa.zip"
        saida.write_bytes(b"antigo")
        codigo, log = self.executa("--ano", "2025", "--saida", str(saida))
        self.assertEqual(codigo, 2)
        self.assertEqual(saida.read_bytes(), b"antigo")
        self.assertEqual(self.servidor.caminhos, [])
        self.assertIn(str(saida), log)

    def test_main_404(self):
        self.servidor.respostas = [resposta(status=404, corpo=b"")]
        codigo, log = self.executa()
        self.assertEqual(codigo, 2)
        self.assertIn("Ano 2025 não publicado no portal", log)
        self.assertFalse(self.destino.parent.exists())

    def test_main_falhas_de_download_saem_com_1(self):
        for falha in (resposta(status=403, corpo=b""),
                      resposta(content_length=len(CORPO) + 100),
                      resposta(corpo=b"<html>erro</html>")):
            with self.subTest(status=falha["status"], corpo=falha["corpo"][:10]):
                self.servidor.respostas = [falha]
                codigo, _ = self.executa()
                self.assertEqual(codigo, 1)
                self.assertFalse(self.destino.exists())

    def test_main_argumentos_invalidos_nao_acessam_a_fonte(self):
        with patch("sys.stderr"), self.assertRaises(SystemExit) as erro:
            coletor.main(["--ano", "25"])
        self.assertEqual(erro.exception.code, 2)
        self.assertEqual(self.servidor.caminhos, [])

    def test_main_configuracao_invalida(self):
        with patch.dict(os.environ, {"PORTAL_DADOS_ABERTOS_URL": "https://x.df.gov.br/base?a=1"}):
            codigo, log = self.executa()
        self.assertEqual(codigo, 2)
        self.assertIn("PORTAL_DADOS_ABERTOS_URL", log)


def bash_compativel():
    """Retorna um bash que aceite caminhos do Windows, ou None.

    No Windows, o primeiro bash do PATH costuma ser o do WSL, que não entende
    caminhos como C:/...; usa-se o do Git for Windows.
    """
    if os.name != "nt":
        return shutil.which("bash")
    for pasta in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432")):
        if pasta and (Path(pasta) / "Git" / "bin" / "bash.exe").exists():
            return str(Path(pasta) / "Git" / "bin" / "bash.exe")
    caminho = shutil.which("bash")
    return caminho if caminho and "\\git\\" in caminho.lower() else None


class TestWrapper(unittest.TestCase):
    @unittest.skipUnless(bash_compativel(), "bash compatível indisponível")
    def test_wrapper_de_outro_diretorio_com_saida_relativa(self):
        # Simula outro repositório chamando o wrapper com um caminho próprio.
        servidor = ServidorLocal()
        self.addCleanup(servidor.fecha)
        ambiente = {**os.environ, "PORTAL_DADOS_ABERTOS_URL": servidor.base,
                    "DOWNLOAD_TIMEOUT_SECONDS": "5"}
        with tempfile.TemporaryDirectory() as tmp:
            processo = subprocess.run(
                [bash_compativel(), (RAIZ / "src" / "coletores" / "run-coleta-empenhos.sh").as_posix(),
                 "--ano", "2025", "--saida", "outputs/despesa.zip"],
                cwd=tmp, env=ambiente, capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(processo.returncode, 0, processo.stdout + processo.stderr)
            self.assertEqual((Path(tmp) / "outputs" / "despesa.zip").read_bytes(), CORPO)
        self.assertEqual(servidor.caminhos, ["/base/Despesa_2025.zip"])


if __name__ == "__main__":
    unittest.main()
