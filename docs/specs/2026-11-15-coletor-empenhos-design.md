# Coletor de empenhos do GDF

Data: 2026-11-15. Status: implementado.

## Objetivo

Baixar, para um ano informado, os empenhos do GDF e salvá-los exatamente como vieram,
para uso no repositório `medicamentos-fdcc`. A coleta deve ser a mais simples e direta
possível; tratamento e cruzamento ficam fora daqui.

## Fontes consideradas

| | SEGES (`comprasnet_contratos`) | Portal da Transparência do DF |
|---|---|---|
| Abrangência | Só unidades federais; nenhuma UG do GDF em 2025 | Só o GDF |
| Atualização | Parou: 2025 vai até 04/08/2025, 2026 até 10/07/2026 | Ano completo; republicado em out/2026 |
| Período | Anual, mensal e diário | Só anual (`Despesa_{ano}.zip`, 2009 a 2026) |

Escolha: o portal do DF. É a única fonte com os empenhos do GDF, incluindo a Secretaria
de Saúde (UG 170101), que não aparece no CSV de contratos da SEGES, e está completa. O
corte mensal seria possível só pela API paginada do portal, o que não é coleta simples.

## Decisões

- Um único parâmetro de período, `--ano`, e o opcional `--saida`, com o mesmo
  comportamento do `coletor-contratos`.
- O ZIP é salvo inteiro, sem descompactar: o coletor não lê o conteúdo, que tem um
  formato difícil (Latin-1, separador `¨`, aspas sem escape).
- Além do `Content-Length`, o arquivo precisa ser reconhecido como ZIP antes de receber
  o nome final, para que uma página de erro servida com 200 não seja salva como dado.
- A pasta do ano só é criada depois da resposta 200, para que um 404 não deixe pasta vazia.
