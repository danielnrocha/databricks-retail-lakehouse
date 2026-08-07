#!/usr/bin/env python3
"""Helpers para gerar arquivos .excalidraw nativos.

Compartilhado por `make_architecture_diagram.py` (visão de negócio) e
`make_technical_diagram.py` (referência técnica). Existe porque a alternativa era duplicar
duzentas linhas de medição de texto e serialização entre dois scripts que mudam juntos.

## Duas decisões que valem a explicação

**A altura da caixa sai do texto já quebrado.** Cada card mede as linhas, soma `PAD` em cima e
embaixo e para por aí — nada de altura fixa com sobra. A largura de caractere é uma *estimativa*,
e ela erra de propósito para baixo: se a estimativa subdimensiona, o Excalidraw quebra em mais
linhas ao abrir e **cresce o container**, e o encaixe continua justo. Se errasse para cima,
sobraria buraco que ninguém corrige depois.

**O ruído é determinístico.** Posição, ângulo e `seed` de cada elemento recebem um deslocamento
pequeno vindo de um `Random` semeado. O desenho não deve parecer saída de gerador — que é
exatamente o que ele é — mas rodar duas vezes tem de produzir o mesmo arquivo, senão todo commit
vira um diff de ruído.
"""

from __future__ import annotations

import json
import random
from collections.abc import Sequence
from pathlib import Path
from typing import Any

Element = dict[str, Any]

# Fontes nativas do Excalidraw. 5 = Excalifont (o traço à mão), 3 = Cascadia (monoespaçada),
# usada só onde o conteúdo É código — nome de opção, SQLSTATE, assinatura de API.
FONT_HAND = 5
FONT_CODE = 3
LINE_HEIGHT = 1.25

INK = "#1e1e1e"
GREY = "#495057"
BLUE = "#1971c2"
GREEN = "#2f9e44"
RED = "#e03131"
ORANGE = "#f08c00"
VIOLET = "#6741d9"

BG_BLUE = "#a5d8ff"
BG_GREEN = "#b2f2bb"
BG_RED = "#ffc9c9"
BG_YELLOW = "#ffec99"
BG_VIOLET = "#d0bfff"
BG_GREY = "#e9ecef"

PAD = 8
NOW = 1786160000000


class Canvas:
    """Acumula elementos e serializa o documento."""

    def __init__(self, seed: int) -> None:
        self.elements: list[Element] = []
        self.rng = random.Random(seed)

    # -- infraestrutura ------------------------------------------------------
    def _seed(self) -> int:
        return self.rng.randint(1, 2**31 - 1)

    def _base(self, kind: str, x: float, y: float, w: float, h: float, **kw: Any) -> Element:
        el: Element = {
            "id": f"e{len(self.elements):03d}{self.rng.randint(1000, 9999)}",
            "type": kind,
            "x": round(x, 2),
            "y": round(y, 2),
            "width": round(w, 2),
            "height": round(h, 2),
            "angle": 0,
            "strokeColor": INK,
            "backgroundColor": "transparent",
            "fillStyle": "solid",
            "strokeWidth": 1,
            "strokeStyle": "solid",
            "roughness": 1,
            "opacity": 100,
            "groupIds": [],
            "frameId": None,
            "roundness": {"type": 3},
            "seed": self._seed(),
            "version": self.rng.randint(20, 400),
            "versionNonce": self._seed(),
            "isDeleted": False,
            "boundElements": [],
            "updated": NOW,
            "link": None,
            "locked": False,
        }
        el.update(kw)
        return el

    # -- medição de texto ----------------------------------------------------
    @staticmethod
    def char_width(size: int, mono: bool = False) -> float:
        # Subdimensionado de propósito; ver o docstring do módulo.
        return size * (0.6 if mono else 0.5)

    def wrap(self, text: str, size: int, max_px: float, mono: bool = False) -> list[str]:
        limit = max(8, int(max_px / self.char_width(size, mono)))
        out: list[str] = []
        for para in text.split("\n"):
            if not para:
                out.append("")
                continue
            line = ""
            for word in para.split(" "):
                trial = f"{line} {word}".strip()
                if len(trial) <= limit:
                    line = trial
                else:
                    if line:
                        out.append(line)
                    line = word
            out.append(line)
        return out

    def measure(self, lines: list[str], size: int, mono: bool = False) -> tuple[float, float]:
        w = max((len(ln) for ln in lines), default=1) * self.char_width(size, mono)
        return w, len(lines) * size * LINE_HEIGHT

    # -- elementos -----------------------------------------------------------
    def label(
        self, x: float, y: float, txt: str, size: int = 20, color: str = INK, mono: bool = False
    ) -> Element:
        lines = txt.split("\n")
        w, h = self.measure(lines, size, mono)
        el = self._base(
            "text",
            x,
            y,
            w,
            h,
            strokeColor=color,
            roundness=None,
            text=txt,
            originalText=txt,
            fontSize=size,
            fontFamily=FONT_CODE if mono else FONT_HAND,
            textAlign="left",
            verticalAlign="top",
            containerId=None,
            lineHeight=LINE_HEIGHT,
            autoResize=True,
        )
        self.elements.append(el)
        return el

    def note(
        self, x: float, y: float, w: float, txt: str, color: str = RED, size: int = 15
    ) -> Element:
        lines = self.wrap(txt, size, w)
        tw, th = self.measure(lines, size)
        body = "\n".join(lines)
        el = self._base(
            "text",
            x,
            y,
            tw,
            th,
            strokeColor=color,
            roundness=None,
            angle=round(self.rng.uniform(-0.011, 0.011), 4),
            text=body,
            originalText=body,
            fontSize=size,
            fontFamily=FONT_HAND,
            textAlign="left",
            verticalAlign="top",
            containerId=None,
            lineHeight=LINE_HEIGHT,
            autoResize=True,
        )
        self.elements.append(el)
        return el

    def card(
        self,
        x: float,
        y: float,
        w: float,
        title: str,
        body: str,
        stroke: str = INK,
        fill: str = "transparent",
        size: int = 15,
        fill_style: str = "solid",
        mono: bool = False,
    ) -> Element:
        inner = w - 2 * PAD
        head = self.wrap(title, size, inner) if title else []
        lines = (
            [*head, "", *self.wrap(body, size, inner, mono)]
            if head
            else self.wrap(body, size, inner, mono)
        )
        _, th = self.measure(lines, size, mono)
        h = th + 2 * PAD

        jx = x + self.rng.uniform(-4, 4)
        jy = y + self.rng.uniform(-3, 3)
        angle = round(self.rng.uniform(-0.008, 0.008), 4)

        box = self._base(
            "rectangle",
            jx,
            jy,
            w,
            h,
            strokeColor=stroke,
            backgroundColor=fill,
            fillStyle=fill_style,
            strokeWidth=self.rng.choice([1, 1, 2]),
            angle=angle,
        )
        raw = "\n".join(lines)
        txt = self._base(
            "text",
            jx + PAD,
            jy + PAD,
            inner,
            th,
            roundness=None,
            angle=angle,
            text=raw,
            originalText=raw,
            fontSize=size,
            fontFamily=FONT_CODE if mono else FONT_HAND,
            textAlign="left",
            verticalAlign="middle",
            containerId=box["id"],
            lineHeight=LINE_HEIGHT,
            autoResize=False,
        )
        box["boundElements"] = [{"type": "text", "id": txt["id"]}]
        self.elements.append(box)
        self.elements.append(txt)
        return box

    def chip(
        self,
        x: float,
        y: float,
        txt: str,
        stroke: str = INK,
        fill: str = "transparent",
        size: int = 14,
    ) -> Element:
        """Etiqueta compacta de UMA linha — para listar produtos e ferramentas.

        Único lugar onde a largura é superdimensionada de propósito (fator 1.18): um chip que
        quebra em duas linhas destrói o alinhamento da fileira inteira, e aqui a sobra horizontal
        custa menos que a quebra.
        """
        w = len(txt) * self.char_width(size) * 1.18 + 2 * PAD + 6
        return self.card(x, y, w, "", txt, stroke, fill, size)

    def frame(self, x: float, y: float, w: float, h: float, color: str = "#adb5bd") -> Element:
        el = self._base(
            "rectangle",
            x,
            y,
            w,
            h,
            strokeColor=color,
            strokeStyle="dashed",
            backgroundColor="transparent",
            angle=round(self.rng.uniform(-0.003, 0.003), 4),
        )
        self.elements.insert(0, el)  # atrás de tudo
        return el

    def arrow(
        self,
        a: Element,
        b: Element,
        color: str = INK,
        dashed: bool = False,
        bow: float = 0.0,
        side: bool = False,
    ) -> Element:
        gap = 6
        if side:
            ax, ay = a["x"] + a["width"] + gap, a["y"] + a["height"] / 2
            bx, by = b["x"] - gap, b["y"] + b["height"] / 2
        else:
            ax, ay = a["x"] + a["width"] / 2, a["y"] + a["height"] + gap
            bx, by = b["x"] + b["width"] / 2, b["y"] - gap
        dx, dy = bx - ax, by - ay
        mid = [dx / 2 + bow + self.rng.uniform(-5, 5), dy / 2 + self.rng.uniform(-4, 4)]
        el = self._base(
            "arrow",
            ax,
            ay,
            dx,
            dy,
            strokeColor=color,
            strokeWidth=2,
            strokeStyle="dashed" if dashed else "solid",
            roundness={"type": 2},
            points=[[0, 0], [round(mid[0], 2), round(mid[1], 2)], [round(dx, 2), round(dy, 2)]],
            lastCommittedPoint=None,
            startBinding={
                "elementId": a["id"],
                "focus": round(self.rng.uniform(-0.08, 0.08), 3),
                "gap": gap,
            },
            endBinding={
                "elementId": b["id"],
                "focus": round(self.rng.uniform(-0.08, 0.08), 3),
                "gap": gap,
            },
            startArrowhead=None,
            endArrowhead="arrow",
            elbowed=False,
        )
        a.setdefault("boundElements", []).append({"id": el["id"], "type": "arrow"})
        b.setdefault("boundElements", []).append({"id": el["id"], "type": "arrow"})
        self.elements.append(el)
        return el

    def stacker(self, columns: Sequence[float], top: float, gap: float = 26.0) -> Stack:
        return Stack(self, columns, top, gap)

    # -- saída ---------------------------------------------------------------
    def save(self, path: Path) -> None:
        doc = {
            "type": "excalidraw",
            "version": 2,
            "source": "https://excalidraw.com",
            "elements": self.elements,
            "appState": {"gridSize": None, "viewBackgroundColor": "#ffffff"},
            "files": {},
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # -- verificação ---------------------------------------------------------
    def audit(self) -> list[str]:
        """Checagens que já pegaram erro real: colisão, texto estourando, ligação órfã.

        Rodar isto é o motivo de o diagrama ser gerado. Um desenho feito à mão só é conferido por
        alguém olhando, e olhar não pega uma seta cruzando um título duas seções abaixo.
        """
        problems: list[str] = []
        by_id = {e["id"]: e for e in self.elements}

        for el in self.elements:
            for bound in el.get("boundElements") or []:
                if bound["id"] not in by_id:
                    problems.append(f"boundElement órfão em {el['id']}")
            if el.get("containerId") and el["containerId"] not in by_id:
                problems.append(f"containerId órfão em {el['id']}")
            if el["type"] == "text" and el.get("containerId"):
                box = by_id[el["containerId"]]
                if el["height"] > box["height"] - 2 * PAD + 1:
                    problems.append(f"texto estoura a caixa {box['id']}")

        solid = [
            e
            for e in self.elements
            if (e["type"] == "rectangle" and e["strokeStyle"] != "dashed")
            or (e["type"] == "text" and not e.get("containerId"))
        ]
        for i, a in enumerate(solid):
            for b in solid[i + 1 :]:
                ix = min(a["x"] + a["width"], b["x"] + b["width"]) - max(a["x"], b["x"])
                iy = min(a["y"] + a["height"], b["y"] + b["height"]) - max(a["y"], b["y"])
                if ix > 4 and iy > 4:
                    problems.append(
                        f"colisão em ({a['x']:.0f},{a['y']:.0f}) x ({b['x']:.0f},{b['y']:.0f})"
                    )

        headings = [e for e in self.elements if e["type"] == "text" and e.get("fontSize", 0) >= 24]
        for ar in (e for e in self.elements if e["type"] == "arrow"):
            xs = [ar["x"] + p[0] for p in ar["points"]]
            ys = [ar["y"] + p[1] for p in ar["points"]]
            for h in headings:
                if (
                    min(xs) < h["x"] + h["width"]
                    and max(xs) > h["x"]
                    and min(ys) < h["y"] + h["height"]
                    and max(ys) > h["y"]
                ):
                    problems.append(f"seta cruza o título {h['text'][:30]!r}")
        return problems


class Stack:
    """Empilha cards por coluna a partir da altura real do anterior.

    Existe porque a primeira versão deste gerador usava offsets fixos (`y + 200`, `y + 230`). Eles
    funcionavam com a estimativa de largura de caractere daquele dia e colidiriam no instante em
    que o Excalidraw crescesse uma caixa ao abrir — que é justamente o comportamento em que o
    dimensionamento se apoia.
    """

    def __init__(self, canvas: Canvas, columns: Sequence[float], top: float, gap: float) -> None:
        self.canvas = canvas
        self.columns = columns
        self.gap = gap
        self.cursor = dict.fromkeys(range(len(columns)), top)

    def put(self, col: int, width: float, title: str, body: str, *args: Any, **kw: Any) -> Element:
        card = self.canvas.card(
            self.columns[col], self.cursor[col], width, title, body, *args, **kw
        )
        self.cursor[col] = card["y"] + card["height"] + self.gap
        return card

    def bottom(self) -> float:
        return max(self.cursor.values()) - self.gap
