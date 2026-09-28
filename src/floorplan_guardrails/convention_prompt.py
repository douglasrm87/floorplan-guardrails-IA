"""A convenção de coordenadas e rotação, única para o mobiliador e o fiscal.

Os dois agentes falam da mesma planta com os mesmos números: o mobiliador
posiciona, o fiscal sugere para onde mover. Na Fase 4b só o mobiliador
conhecia a convenção, e o fiscal sugeriu rotação com a frente para o lado
errado. Por isso o texto mora aqui, e as instruções dos dois o incluem
inteiro: mudar a convenção é mudar um lugar só.

Na Fase 4c o fiscal ainda escolhia a parede certa e a rotação que deixava a
frente contra ela: sabia para onde a frente aponta em cada rotação, mas
errava a conta inversa. Por isso a convenção diz também qual rotação encosta
o fundo em cada parede.

O que pode entrar: o referencial da casa e o do móvel, a rotação, a posição
como canto da pegada já girada e o que é a geometria calculada que chega na
mensagem. O que não pode: nenhum valor de `config/furniture_rules.yaml`. Os
testes das duas instruções varrem este texto junto com o resto.
"""

# O texto é prosa, como as instruções dos agentes: quebrar as frases em 88
# colunas pioraria o que o autor edita e o que o modelo lê.
# ruff: noqa: E501

CONVENTION = """Coordenadas da casa, em metros e com duas casas decimais:
- A origem fica no canto inferior esquerdo da casa. x cresce para leste, y cresce para norte.
- Cada cômodo é um retângulo alinhado aos eixos: x e y são o canto inferior esquerdo, width é a extensão em x e depth é a extensão em y.
- As portas são vãos nas paredes. O offset de uma porta é medido a partir do canto de menor coordenada da parede: do oeste nas paredes norte e sul, do sul nas paredes leste e oeste.

Referencial do móvel:
- Sem rotação, width do catálogo corre em x e depth corre em y.
- A frente (front) do móvel é o lado sul, o fundo (back) é o norte, a esquerda (left) é o oeste e a direita (right) é o leste.

Rotação, em graus e no sentido anti-horário, só 0, 90, 180 ou 270:
- A frente aponta para o sul em 0, para o leste em 90, para o norte em 180 e para o oeste em 270. Fundo, esquerda e direita giram junto.
- Em 90 e 270 o móvel fica deitado de lado: a extensão em x passa a ser o depth do catálogo, e a extensão em y passa a ser o width.
- Para encostar o fundo de um móvel numa parede, use a rotação que deixa a frente para o lado oposto:
  - fundo encostado na parede norte: frente para o sul, rotação 0;
  - fundo encostado na parede oeste: frente para o leste, rotação 90;
  - fundo encostado na parede sul: frente para o norte, rotação 180;
  - fundo encostado na parede leste: frente para o oeste, rotação 270.
- A frente de um móvel encostado a uma parede nunca fica voltada para essa mesma parede.

Posição:
- x e y de um móvel são o canto inferior esquerdo do retângulo que ele ocupa no piso, já girado, em coordenadas absolutas da casa. Não faça conta de rotação para posicionar: gire, veja qual é a extensão em x e em y, e ponha o canto inferior esquerdo onde quer.
- O móvel fica inteiro dentro do cômodo quando x mais a extensão em x não passa do limite leste do cômodo, y mais a extensão em y não passa do limite norte, e x e y não ficam antes dos limites oeste e sul.

Geometria calculada:
- A mensagem traz a geometria que o código calculou a partir da planta: os limites de cada cômodo, o vão de cada porta com o cômodo para onde cada face abre e, quando há móveis posicionados, o retângulo que cada um ocupa e a direção da frente. Use esses números em vez de refazer as contas."""
