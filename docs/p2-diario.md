# Diário do P2

Registro do que se observou durante a construção do P2 e não virou mudança
de código. Cada entrada tem data, o fato medido e o que foi decidido.

## 28/09/2026: uma planta aprovada em 21/09 reprova hoje

A execução `runs/20260921T021959Z-6e44f8.jsonl` terminou aprovada na
iteração 3, em 21/09/2026. Validada de novo hoje com `config/rules.yaml`, a
mesma planta tem uma violação:

> O cômodo "Banheiro" (r4) tem 1,00 m na menor dimensão, abaixo do mínimo de
> 1,10 m exigido para banheiros.

A causa é a mudança de regra do PR #18 (`fix(rules): cite each source and use
the nbr bathroom width`), que passou a largura mínima do banheiro para 1,10 m,
depois da execução.

Consequências:

- Um registro em `runs/` diz que a planta passou nas regras **daquele dia**,
  não nas de hoje. Nenhuma planta de `runs/` entra em `examples/p2/` sem ser
  validada de novo.
- O P2 já trata isso: `require_approved_plan` valida a planta de entrada com
  as regras atuais, e esta planta seria recusada com `InvalidInputPlan`.
- Ela ficou fora da escolha de `examples/p2/plans/aprovada_gerada.json`, que
  saiu de `runs/20260921T022056Z-0e2b4f.jsonl`, iteração 2.

## 28/09/2026: o validador do P1 aceita porta interna declarada duas vezes

A planta aprovada de `runs/20260921T020607Z-67b064.jsonl` tem 9 portas para 5
vãos: cada porta interna aparece uma vez em cada um dos dois cômodos que liga,
na mesma posição (por exemplo, `r1` parede leste para `r2` e `r2` parede oeste
para `r1`). O validador do P1 aprova a planta, porque nenhuma regra proíbe
duas declarações do mesmo vão.

No P2, cada declaração é medida como uma porta. Com uma cama a 0,20 m do vão
entre a sala e o quarto dessa planta, `inspect` devolve a mesma violação de
`door_clearance` duas vezes, com a mesma mensagem. O desenho também traça as
faixas dessa porta duas vezes, uma sobre a outra.

Decisão: **sem correção**. O validador do P1 é intocável no P2
(`PLAN-P2.md`, seção 2), e as plantas de `examples/p2/` declaram cada vão uma
vez só. Fica como limite conhecido: uma planta gerada pelo aluno no P1 pode
chegar ao P2 com violações repetidas.

## 28/09/2026: a casa em L é aprovada no P1 e inviável com cadeira de rodas

A planta `examples/p2/plans/casa_em_l.json` passa no validador do P1 sem
nenhuma violação. Com o perfil acessível, porém, ela não tem solução: o
banheiro (`d`) mede 4,00 × 1,20 m, e o giro de cadeira de rodas exige um
quadrado livre de 1,50 m de lado. Nem vazio o banheiro comporta o giro.

Na medição da Fase 5 (`docs/avaliacao-p2.md`), as duas negociações dessa
combinação gastaram as 3 rodadas, 124,5 s e 104,5 s, e terminaram em
`not_converged`. O mobiliador não tinha como sair: a omissão é por móvel, e
o problema não é de nenhum móvel.

Decisão: a negociação ganhou o estado `infeasible`. Com o perfil acessível,
antes da primeira chamada ao modelo, o código confere se cada cômodo em que o
giro é verificado comporta o giro vazio. Se algum não comporta, a negociação
termina ali, sem chamar modelo nenhum, com uma mensagem por cômodo dizendo
que a planta precisa voltar à etapa de geração. É o exercício 1 do nível 3 do
notebook.

## 28/09/2026: 30 das 57 sugestões adotadas falharam

Na mesma medição, o fiscal escreveu 66 sugestões com posição final e com
rodada seguinte. O mobiliador adotou 57 ao pé da letra (86%). Em 30 dessas
57, o móvel movido voltou a ser citado numa violação na rodada seguinte: 18
por uma regra que não o citava antes (a sugestão criou o problema) e 12 pela
mesma regra (a sugestão não resolveu).

O caso mais claro está gravado em
`examples/p2/replays/sugestao-errada.jsonl`: o fiscal sugere o guarda-roupa
encostado na parede sul, atravessando a porta do quarto, e na rodada
seguinte o código mede 0,00 m livres diante dela.

O número é o argumento do projeto medido: a sugestão do fiscal é uma
proposta, e só vale depois que o código mede de novo. Ele entrou no notebook
como pergunta de debate do exercício 4 do nível 3.
