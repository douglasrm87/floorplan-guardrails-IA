# Relatório de medição do P2

Este relatório mede a negociação inteira do P2, mobiliador, verificação e
fiscal, sobre as quatro plantas de `examples/p2/plans/`. As perguntas são as
da Fase 5: quanto tempo o aluno espera, como as negociações terminam e o que
acontece com as sugestões do fiscal.

A resposta curta: **o perfil padrão aprova metade das vezes, o perfil
acessível não aprovou nenhuma, e mais da metade das sugestões que o
mobiliador seguiu ao pé da letra falhou na rodada seguinte.**

## Como a rodada foi feita

- **Data:** 28/09/2026, 03:49 a 04:10 UTC.
- **Deployment:** `gpt-5-mini`, esforço de raciocínio `low` nos dois agentes.
- **Limite de rodadas:** 3 (`DEFAULT_MAX_ROUNDS`).
- **Execuções:** 16, que são 4 plantas vezes 2 perfis vezes 2 repetições.
- **Ordem:** em série, uma negociação de cada vez. Dentro de cada repetição,
  plantas e perfis se alternam, para que uma lentidão passageira do deployment
  não caia inteira sobre uma planta só.
- **Configuração:** `config/rules.yaml`, `config/furniture_catalog.yaml` e
  `config/furniture_rules.yaml` como estavam em 28/09/2026.
- **Pedidos:** os de `examples/p2/requests/`, iguais nas quatro plantas: sala
  com sofá, cozinha com geladeira e fogão, quarto com cama de casal e
  guarda-roupa, banheiro com vaso e box.
- **Script:** `eval/run_p2.py`, com as contas em `eval/report_p2.py`.

A rodada rodou sobre o código de `p2` em `63d33c8`, **antes** de duas mudanças
que ela mesma motivou (ver "O que mudou depois da medição"). Duas repetições
é pouco: a avaliação do P1 mostrou que o ruído entre repetições pesa mais do
que qualquer mudança de regra, e nada aqui foi medido duas vezes nas mesmas
condições além das duas repetições.

## Como as negociações terminaram

| Planta | n | approved | declined | not_converged |
|---|---|---|---|---|
| aprovada_gerada | 4 | 1/4 | 0/4 | 3/4 |
| casa_4_comodos | 4 | 2/4 | 0/4 | 2/4 |
| casa_em_l | 4 | 1/4 | 1/4 | 2/4 |
| quarto_apertado | 4 | 0/4 | 2/4 | 2/4 |
| **total** | 16 | 4/16 (25%) | 3/16 (19%) | 9/16 (56%) |

| Perfil | n | approved | declined | not_converged |
|---|---|---|---|---|
| padrão | 8 | 4/8 (50%) | 2/8 (25%) | 2/8 (25%) |
| acessível | 8 | 0/8 (0%) | 1/8 (12%) | 7/8 (88%) |

Nenhuma negociação com cadeira de rodas foi aprovada. Em uma das plantas isso
nem depende do mobiliador: o banheiro da `casa_em_l` tem 1,20 m de largura, e
o giro de 1,50 m não cabe nele nem vazio. As duas negociações dessa
combinação gastaram 3 rodadas cada uma, 124,5 s e 104,5 s, num problema que
nenhuma disposição resolve.

## Execução a execução

| # | planta | perfil | rep. | final | violações por rodada | total |
|---|---|---|---|---|---|---|
| 1 | aprovada_gerada | padrão | 1 | not_converged | 3 → 1 → 3 | 76,8 s |
| 2 | aprovada_gerada | acessível | 1 | not_converged | 2 → 2 → 3 | 94,9 s |
| 3 | casa_4_comodos | padrão | 1 | approved | 0 | 17,6 s |
| 4 | casa_4_comodos | acessível | 1 | not_converged | 2 → 3 → 3 | 88,5 s |
| 5 | casa_em_l | padrão | 1 | declined | 3 → desistiu | 45,8 s |
| 6 | casa_em_l | acessível | 1 | not_converged | 7 → 8 → 6 | 124,5 s |
| 7 | quarto_apertado | padrão | 1 | not_converged | 3 → 2 → 3 | 115,3 s |
| 8 | quarto_apertado | acessível | 1 | declined | 6 → desistiu | 71,5 s |
| 9 | aprovada_gerada | padrão | 2 | approved | 0 | 14,7 s |
| 10 | aprovada_gerada | acessível | 2 | not_converged | 5 → 4 → 3 | 123,0 s |
| 11 | casa_4_comodos | padrão | 2 | approved | 2 → 0 | 51,6 s |
| 12 | casa_4_comodos | acessível | 2 | not_converged | 2 → 2 → 3 | 69,8 s |
| 13 | casa_em_l | padrão | 2 | approved | 9 → 2 → 0 | 104,0 s |
| 14 | casa_em_l | acessível | 2 | not_converged | 5 → 4 → 6 | 104,5 s |
| 15 | quarto_apertado | padrão | 2 | declined | desistiu na 1ª | 21,1 s |
| 16 | quarto_apertado | acessível | 2 | not_converged | 4 → 4 → 5 | 129,0 s |

Nas 16 negociações, o fiscal chamou `consultar_parametro` 97 vezes, nenhuma
com id inválido. A verificação de números trocou 13 textos do fiscal pela
mensagem do código.

## Quanto o aluno espera

| Medida | n | mediana | p90 |
|---|---|---|---|
| Negociação inteira | 16 | 82,7 s | 124,5 s |
| Rodada, qualquer uma | 39 | 33,0 s | 46,3 s |
| Rodada 1 | 16 | 40,1 s | 49,1 s |
| Rodada 2 | 13 | 22,3 s | 42,1 s |
| Rodada 3 | 10 | 35,0 s | 45,3 s |
| Mobiliador, por chamada | 39 | 15,2 s | 22,5 s |
| Fiscal, por chamada | 32 | 22,1 s | 29,9 s |

O p90 é o do posto mais próximo: um valor que de fato ocorreu. O fiscal
demora mais do que o mobiliador, porque lê a mensagem maior (8.852 tokens de
entrada na mediana, contra 4.532) e consulta a ferramenta antes de responder.
É daqui que saem os tempos do notebook: mobiliador cerca de 15 s, fiscal de
20 a 30 s, negociação de 1 a 2 minutos.

`DEFAULT_MAX_ROUNDS` continua em 3. Com a mediana de 82,7 s, uma quarta
rodada levaria a p90 para perto de 3 minutos, e as negociações que não
convergiram não mostraram tendência de queda nas violações que justificasse
esperar mais.

## As sugestões do fiscal

O fiscal escreve a sugestão como posição e rotação finais ("m4 em x = 2.40,
y = 3.00, rotação 90"). Uma sugestão conta como **adotada** quando a proposta
da rodada seguinte põe aquele móvel exatamente ali.

| Medida | Valor |
|---|---|
| Sugestões com posição escrita e rodada seguinte | 66 |
| Adotadas ao pé da letra | 57/66 (86%) |
| Adotadas cujo móvel foi citado na rodada seguinte | 30/57 (53%) |
| ...por uma regra que não o citava antes | 18/57 (32%) |
| ...pela mesma regra de antes | 12/57 (21%) |
| Sugestões sem posição escrita (fora da conta) | 8 |

O mobiliador segue o fiscal quase sempre, e o fiscal erra mais da metade das
vezes. Em 18 casos a sugestão criou um problema novo: o guarda-roupa sugerido
na frente de uma porta, o sofá sugerido rente à porta de entrada. Em outros
12, o móvel foi para onde o fiscal disse e continuou reprovado pela mesma
regra.

A conta subestima o efeito: o giro de cadeira de rodas não cita móvel, então
uma sugestão que tira o giro de um cômodo não aparece como causadora. Isso
aconteceu pelo menos uma vez, na negociação gravada em
`examples/p2/replays/sugestao-errada.jsonl`.

É o argumento do projeto medido: a sugestão do fiscal é uma proposta, e só
vale depois que o código mede de novo.

## O que mudou depois da medição

- **Estado `infeasible`.** Com o perfil acessível, a negociação confere antes
  da primeira chamada se cada cômodo em que o giro é verificado comporta o
  giro vazio. Se algum não comporta, ela termina em `infeasible` sem chamar
  modelo nenhum, com uma mensagem por cômodo. As execuções 6 e 14 terminariam
  assim, em zero segundos.
- **Identificadores de parâmetro no texto do fiscal.** Nas explicações
  aparecia, por exemplo, "parâmetro door_clearance_depth = 0.8 m". O código
  agora troca cada id de parâmetro pela descrição dele, e as instruções do
  fiscal pedem vírgula decimal na explicação e no resumo.

Nenhuma das duas mudanças foi medida numa rodada nova.

## Como repetir

```bash
uv run --env-file .env python -m eval.run_p2
uv run python -m eval.run_p2 --report eval/results/p2-<data>
```

A primeira roda as 16 negociações e grava os registros em `runs/p2/` e o
relatório em `eval/results/p2-<data>/`, que o git ignora. A segunda refaz o
relatório a partir dos registros, sem chamar o modelo.
