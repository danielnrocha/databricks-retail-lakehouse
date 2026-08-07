# Diagramas

## `arquitetura-plataforma.excalidraw`

Visão didática da plataforma em uma página: por que ela existe, de onde vem o dado, o caminho
bronze → prata → ouro, o que as pessoas realmente consomem, o que foi conscientemente abandonado
e o que impede tudo isso de apodrecer.

Abra em [excalidraw.com](https://excalidraw.com) (*Menu → Open*) ou na extensão do VS Code.

**É um artefato gerado, não desenhado.** O texto e a diagramação vivem em
`scripts/make_architecture_diagram.py`; o `.excalidraw` é a saída. Para mudar qualquer coisa, edite
o script e rode:

```bash
python3 scripts/make_architecture_diagram.py
```

Editar o `.json` à mão funciona uma vez e depois é sobrescrito — se você fizer isso, traga a
mudança de volta para o script.

O motivo de gerar em vez de desenhar é o mesmo que levou `scripts/publish_governance.py` a existir:
um artefato que só existe porque alguém o montou uma vez não pode ser refeito, revisado em diff,
nem reproduzido por outra pessoa. Ver a entrada de 2026-08-07 em `docs/decision-log.md` sobre a
política de tags que existia apenas no workspace.

### Estilo

Tudo nativo do Excalidraw, sem CSS nem tema custom: fonte **Excalifont** (`fontFamily: 5`), traço
desenhado à mão (`roughness: 1`), paleta e setas padrão. Posição, ângulo e semente de cada elemento
recebem um ruído pequeno e determinístico — o desenho não deve parecer saída de gerador, que é
exatamente o que ele é. A altura de cada caixa é calculada a partir do texto já quebrado, com 8px
de margem, para não sobrar espaço vazio.
