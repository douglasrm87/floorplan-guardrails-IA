# O modelo escolhe e explica, o código mede e decide

Material de apoio da segunda oficina. Serve a quem esteve presente, como
revisão, e a quem não esteve, como leitura autônoma: o texto não pressupõe o
encontro. Ele parte do [guia da primeira oficina](oficina.md), e convém ler
aquele antes.

## O problema

A primeira oficina terminou com uma planta aprovada e uma lista do que a
ferramenta não fazia. Mobiliário estava na lista. Esta oficina começa ali: a
planta existe, e agora é preciso pôr os móveis dentro dela.

Arrumar móveis parece assunto de gosto, e em parte é. Mas uma parte grande não
é: uma cama diante da porta impede a porta de abrir, um guarda-roupa com as
portas voltadas para a parede não serve para nada, e um quarto em que a cadeira
de rodas não consegue girar não serve a quem usa cadeira. Essas perguntas têm
resposta, e a resposta é uma medida.

A novidade em relação à primeira oficina é que agora há dois modelos. Um propõe
a disposição; o outro escreve o parecer. A tentação é deixar o segundo julgar o
primeiro, e esse é justamente o caminho que a primeira oficina descartou: pedir
a outro modelo que julgue troca um juízo incerto por dois. A pergunta desta
oficina é outra: onde cabe um segundo modelo sem que ele fique com o veredito?

## O princípio

**O LLM escolhe e explica; o código mede e decide.**

São três papéis:

- o **mobiliador** é um agente que escolhe onde cada móvel fica, a partir de um
  catálogo com dimensões fixas;
- o **código** confere a disposição e diz o que falhou, quanto se mediu e
  quanto se exige;
- o **fiscal** é outro agente, que lê o que o código mediu e redige o parecer
  em português: explica cada falha e sugere uma correção.

Na prática, o princípio vira três restrições.

**O modelo nunca produz um número que o código já conhece.** O mobiliador
devolve o identificador do móvel no catálogo, a posição e a rotação; a largura
e a profundidade vêm do catálogo. O fiscal devolve texto; o medido, o exigido e
a fonte vêm do código. Se o fiscal escrever na explicação um número que o
código não forneceu, o código troca a explicação inteira pela mensagem
original. O modelo não consegue inventar uma medida.

**Nenhum dos dois agentes conhece as exigências.** As instruções do mobiliador
e do fiscal não contêm nenhum valor de `config/furniture_rules.yaml`, e um
teste falha se algum deles aparecer ali. O mobiliador descobre quanto espaço a
porta precisa lendo o parecer, como o gerador da primeira oficina descobria a
área mínima de um quarto lendo o relatório.

**O veredito é do código.** O fiscal não tem como aprovar nem reprovar: o
formato da resposta dele nem tem campo para isso. O mobiliador não tem como
contestar o parecer: só pode reposicionar os móveis ou declarar que algum não
cabe.

### Por que a sugestão do fiscal não basta

O fiscal recebe a geometria de todos os cômodos, a convenção de rotação e os
números medidos, e escreve a sugestão como posição final ("m5 em x = 0.00,
y = 3.00, rotação 180"). Mesmo assim ele erra, e erra muito.

Numa medição com 16 negociações, em 28/09/2026, com o deployment `gpt-5-mini`,
o fiscal escreveu 66 sugestões com posição, e o mobiliador adotou 57 delas ao
pé da letra. Das 57 sugestões adotadas, 30 falharam na rodada seguinte (18
criaram violação nova, 12 não resolveram), segundo o
[relatório](avaliacao-p2.md).

Um desses casos ficou gravado em `examples/p2/replays/sugestao-errada.jsonl`.
Na primeira rodada, o fiscal sugeriu encostar o guarda-roupa na parede sul do
quarto. O mobiliador obedeceu, e na segunda rodada o código mediu:

> O móvel "Guarda-roupa" (m5) deixa só 0,00 m livres diante da porta na parede
> sul do cômodo "Quarto" (r3); são exigidos 0,80 m.

O guarda-roupa foi parar na frente da porta. É por isso que a sugestão aparece
no notebook como "sugestão do fiscal, verificada na próxima rodada": ela é uma
proposta, e só vale depois que o código mede de novo.

## Como a disposição é representada

O mobiliador não devolve um desenho: devolve uma lista de posicionamentos, cada
um com um identificador (`m1`, `m2`...), o cômodo, o móvel do catálogo, a
posição `x`, `y` e a rotação. As coordenadas são as da primeira oficina: metros,
origem no canto inferior esquerdo da casa, `x` para leste e `y` para norte.

Sem rotação, a largura do móvel fica em `x`, a profundidade em `y`, e a
**frente** aponta para o sul. A rotação só pode ser 0, 90, 180 ou 270 graus, no
sentido anti-horário, e a frente aponta respectivamente para sul, leste, norte e
oeste. A posição é o canto inferior esquerdo do retângulo que o móvel ocupa **já
girado**: assim o modelo nunca precisa fazer conta de rotação para posicionar.

Se um móvel pedido não couber de jeito nenhum, o mobiliador pode declarar uma
omissão, com o motivo numa frase. Omissão encerra a negociação: é uma
desistência dita com todas as letras.

## O que o código mede

Dez verificações, em dois grupos. Todas rodam a cada rodada, não apenas as que
falharam antes, pelo mesmo motivo da primeira oficina: mexer num móvel pode
quebrar outro.

As de **integridade** perguntam se a proposta fecha como desenho, e não leem
número nenhum de arquivo:

- nenhum identificador de posicionamento repetido;
- o cômodo existe na planta;
- o móvel existe no catálogo;
- o móvel pode ficar naquele tipo de cômodo (não há fogão no quarto);
- o móvel está inteiro dentro do cômodo;
- nenhum móvel ocupa o espaço de outro (encostar pode);
- o pedido foi atendido, sem faltar nem sobrar: por cômodo, o que foi posto
  mais o que foi omitido é exatamente o que se pediu.

As de **circulação** leem os parâmetros de `config/furniture_rules.yaml`:

- **espaço diante da porta**: o vão de cada porta precisa de uma profundidade
  livre para dentro de cada cômodo que ela liga, 0,80 m;
- **faixa de uso**: cada lado de um móvel que se usa precisa de uma faixa livre
  diante dele, com profundidade própria de cada móvel (0,50 m diante do
  guarda-roupa, por exemplo). A cama de casal exige os dois lados livres; a de
  solteiro, só um;
- **giro de cadeira de rodas**: quando o morador usa cadeira, cada quarto,
  banheiro e sala precisa de um espaço livre para um giro de 1,50 m de
  diâmetro.

As duas primeiras se reduzem a uma única pergunta, respondida por uma função:
a partir de um lado (o vão da porta, a frente do guarda-roupa), quanto espaço
livre existe até o primeiro obstáculo ou até a parede? Nem todo móvel conta
como obstáculo: a mesa de cabeceira, por exemplo, não atrapalha a faixa de uso
da cama.

Sobre uma disposição feita à mão para falhar, `examples/p2/proposals/layout_ruim.json`,
as mensagens são estas:

> O móvel "Cama de casal" (m4) deixa só 0,35 m livres diante da porta na parede
> sul do cômodo "Quarto" (r3); são exigidos 0,80 m.
>
> O móvel "Guarda-roupa" (m5) tem 0,00 m livres na frente (lado leste), até a
> parede; são exigidos 0,50 m.

Com o morador em cadeira de rodas, a mesma disposição ganha uma terceira:

> O cômodo "Quarto" (r3) não tem espaço de giro para cadeira de rodas: o maior
> quadrado livre tem 1,25 m de lado, e são exigidos 1,50 m.

Cada violação de circulação carrega a fonte do parâmetro que ela cobrou. O
fiscal não sabe os números de cor: para citar a fonte, ele chama uma
ferramenta, `consultar_parametro`, que lê o mesmo arquivo.

### O giro: um quadrado no lugar do círculo

A referência de acessibilidade fala em um círculo de giro. O código não procura
um círculo: procura um **quadrado livre com lado igual ao diâmetro**. Se o
quadrado cabe, o círculo cabe dentro dele.

A aproximação é conservadora, porque o quadrado pede mais espaço do que o
círculo. O código pode reprovar um cômodo em que o círculo caberia, mas nunca
aprova um em que ele não cabe. A busca também anda numa grade de 5 cm, e um
quadrado que só caberia fora da grade não é encontrado; esse erro vai para o
mesmo lado, o da reprovação.

A troca vale a pena por causa da legibilidade. Um quadrado alinhado aos eixos
se confere com a mesma aritmética de intervalos que o resto do verificador usa,
sem biblioteca de geometria. Quando falha, a mensagem diz o lado do maior
quadrado encontrado, e o desenho o mostra com o círculo inscrito.

## Uma planta aprovada que não serve

A planta `examples/p2/plans/casa_em_l.json` passa no verificador da primeira
oficina sem nenhuma violação. O banheiro dela mede 4,00 × 1,20 m, acima da
largura mínima de 1,10 m que as regras da primeira oficina exigem para
banheiros.

Para quem usa cadeira de rodas, porém, essa planta não tem solução. O giro
exige um quadrado livre de 1,50 m de lado, e o banheiro não o comporta nem
vazio. Nenhuma disposição de móveis resolve isso.

A primeira versão da negociação não sabia disso. Na medição, as duas
negociações dessa planta com cadeira de rodas gastaram as três rodadas, uma em
124,5 s e outra em 104,5 s, e terminaram sem acordo. O mobiliador não tinha
como sair: a omissão é por móvel, e o problema não era de móvel nenhum.

Daí veio o quarto estado final. Com cadeira de rodas, antes de chamar qualquer
modelo, o código confere se cada cômodo em que o giro é verificado comporta o
giro vazio. Se algum não comporta, a negociação termina ali:

> O cômodo "Banheiro" (d) não tem espaço de giro para cadeira de rodas nem
> vazio: o maior quadrado que cabe nele tem 1,20 m de lado, e são exigidos
> 1,50 m. Nenhuma disposição de móveis resolve isso: a planta precisa voltar à
> etapa de geração.

O caso ensina duas coisas.

**Aprovada quer dizer aprovada nas regras que existiam.** A primeira oficina não
tinha regra de acessibilidade, então uma planta correta para ela pode ser
inútil para um morador em cadeira de rodas. É o mesmo limite com que a primeira
oficina terminou, visto de outro ângulo: a verificação é tão boa quanto a
especificação.

**Reconhecer o limite é um desfecho, não uma falha.** Quando o problema está na
etapa anterior, a resposta certa é dizer isso, e não gastar rodadas tentando.
Uma negociação boa termina em um de três lugares: aprovar, desistir ou
reconhecer o limite.

## Os quatro desfechos

- `approved`: nenhuma verificação falhou;
- `declined`: o mobiliador declarou que algum móvel não cabe;
- `not_converged`: as rodadas acabaram, três por padrão, e ainda havia
  violações;
- `infeasible`: com cadeira de rodas, algum cômodo não comporta o giro nem
  vazio. Nenhum modelo é chamado.

Só o último é previsível. Os outros três dependem do modelo, que varia de uma
execução para outra: na medição, o perfil padrão aprovou 4 de 8 negociações, e
o perfil acessível nenhuma. Com duas repetições por combinação, esses números
mostram a ordem de grandeza, não uma taxa.

## Limites

O que a ferramenta **não** faz:

- folha de porta, sentido de abertura e arco de varredura: a porta aqui é só um
  vão na parede, como na primeira oficina;
- conferir se um móvel bloqueia uma janela;
- móveis que não sejam retangulares, ou girados fora de 0, 90, 180 e 270 graus;
- rota acessível entre cômodos: o giro de cadeira de rodas é verificado só
  dentro de cada cômodo, e nada garante que a cadeira chegue até lá;
- mobiliário mínimo obrigatório por norma: quem diz que móveis entram é o
  pedido;
- altura, pé-direito e bancadas em L;
- consulta a texto de norma: os parâmetros vêm de um arquivo escrito à mão, e
  a ferramenta do fiscal lê esse arquivo, não a norma.

Os parâmetros de circulação são **didáticos e provisórios**. O campo `source` de
cada um, em `config/furniture_rules.yaml`, diz de onde veio o número e até onde
ele foi conferido; o diâmetro de giro vem da ABNT NBR 9050 por fonte
secundária. As dimensões do catálogo são medidas usuais de mercado, também
provisórias.

Uma ressalva para quem é de engenharia civil ou arquitetura: estes números
servem para mostrar o método, não para conferir projeto. Acessibilidade segue a
NBR 9050 e a legislação do lugar da obra, e responde a um profissional
habilitado.

Material didático. **Não substitui projeto de profissional habilitado.**

## Executando por conta própria

O botão *Open in GitHub Codespaces*, no início do [README](../README.md), abre o
ambiente pronto no navegador. O notebook `notebooks/oficina-p2.ipynb` roda de
cima para baixo e pede as mesmas três informações do Azure que o primeiro; a
seção "Qual endereço copiar do portal" do [guia da primeira oficina](oficina.md)
diz onde achar cada uma.

Os tempos, medidos: cada chamada do mobiliador leva cerca de 15 s, e cada
parecer do fiscal, de 20 a 30 s. Uma negociação inteira leva normalmente de 1 a
2 minutos, e em cerca de 1 a cada 5 execuções passa de 2 minutos. Se a conexão
cair, o notebook desenha negociações gravadas em `examples/p2/replays/` sem
chamar o modelo.

Quatro exercícios, na ordem do notebook:

1. Negocie a `casa_em_l` com cadeira de rodas. É o único desfecho que dá para
   prever: `infeasible`, na hora.
2. Negocie o `quarto_apertado`, cujo quarto tem 2,40 × 3,40 m, com cama de casal
   e guarda-roupa. Observe se o mobiliador consegue ou desiste.
3. Negocie a `casa_4_comodos` com cadeira de rodas e compare com o perfil
   padrão.
4. Abra a gravação `sugestao-errada` e procure a rodada em que a sugestão do
   fiscal criou o problema.

Depois, mude um número em `config/furniture_rules.yaml`, o diâmetro de giro ou
a faixa de uso do guarda-roupa, e repita uma negociação. Os modelos são os
mesmos; a exigência é que mudou, e o mobiliador só percebe pelo parecer.

## Levando o padrão adiante

A primeira oficina mostrou um modelo gerando e o código verificando. Esta
acrescenta um segundo modelo sem mudar quem decide: um escolhe, outro explica,
e nenhum dos dois tem a palavra final.

A divisão serve fora das plantas. Um modelo pode explicar por que um teste
falhou, mas quem diz se ele falhou é o teste. Um modelo pode sugerir a
correção de uma consulta, mas quem diz se ela está certa é o banco, executando
a consulta de novo. Sempre que houver dois modelos conversando, vale perguntar
qual deles tem o veredito. A resposta que esta oficina defende: nenhum. O
veredito fica com o que se consegue medir.
