import math
import re
import sys
import time
from dataclasses import dataclass
from typing import Any

import brain_util as bu


# === CONFIG — change this section to switch tasks ===

@dataclass(frozen=True, slots=True)
class TaskConfig:
    region: str = "NONE"
    scale: float = 1.0
    agent: str = "chess"
    parser_agent: str = "parser"
    grid_size: int = 8
    grid_color: str = "rgba(0,255,200,0.95)"
    grid_stroke_width: int = 4
    arrow_color: str = "rgba(255,60,60,0.9)"
    arrow_stroke_width: int = 3
    agent_max_tokens: int = 200
    parser_max_tokens: int = 30
    post_action_delay: float = 5.0


AGENT_SYSTEM: str = """\
You are a chess engine playing as White. White pieces are at the bottom.
Red arrow on the image marks your last move with labeled squares — avoid repeating it.
Reply format: move FROM TO (e.g. move e2 e4). Nothing else.\
"""

AGENT_USER: str = """\
IT IS YOUR TURN. You must act now or the game will be lost.
{context}Reply with only the move.\
"""

PARSER_SYSTEM: str = """\
You are a Python programmer. You have exactly one function:
  drag('FROM', 'TO')
FROM and TO are chess squares like 'e2', 'g1', 'f3'.
Convert the user's move request into a single drag() call. Nothing else.\
"""

PARSER_USER: str = """\
{raw_text}\
"""


# === OVERLAY BUILDER — task-specific visual aids ===

def build_overlays(cfg: TaskConfig, context: dict[str, Any]) -> list[dict[str, Any]]:
    overlays = _make_grid_overlays(cfg.grid_size, cfg.grid_color, cfg.grid_stroke_width)
    last_move = context.get("last_move", "")
    from_sq = last_move[:2] if len(last_move) >= 4 else ""
    to_sq = last_move[2:4] if len(last_move) >= 4 else ""
    overlays.extend(_make_arrow_overlay(from_sq, to_sq, cfg.arrow_color, cfg.grid_size, cfg.arrow_stroke_width))
    return overlays


def build_user_message(cfg: TaskConfig, context: dict[str, Any]) -> str:
    last_move = context.get("last_move", "")
    ctx = f"Your last move was {last_move}. Make a different legal move with a White piece. " if last_move else "Make a legal move with a White piece to advance your position. "
    return AGENT_USER.format(context=ctx)


# === RESPONSE PARSER — exec() the parser VLM output ===

def exec_action(cfg: TaskConfig, code: str, context: dict[str, Any]) -> None:
    clean = re.sub(r'<think>.*?</think>', '', code, flags=re.DOTALL)
    clean = re.sub(r'^```\w*\n?|```$', '', clean.strip(), flags=re.MULTILINE).strip()
    moved: list[str] = []

    def drag(fr: str, to: str) -> None:
        fr, to = fr.strip().lower(), to.strip().lower()
        from_x, from_y = _uci_to_norm(fr, cfg.grid_size)
        to_x, to_y = _uci_to_norm(to, cfg.grid_size)
        bu.device(cfg.agent, cfg.region, [
            {"type": "drag", "x1": from_x, "y1": from_y, "x2": to_x, "y2": to_y}
        ])
        moved.append(f"{fr}{to}")

    try:
        exec(clean, {"__builtins__": {}}, {"drag": drag})
    except Exception:
        pass
    if moved:
        context["last_move"] = moved[-1]


# === PIPELINE — capture → annotate → VLM → parse → act ===

def run_step(cfg: TaskConfig, context: dict[str, Any]) -> None:
    base_b64 = bu.capture(cfg.agent, cfg.region, scale=cfg.scale)
    if not base_b64:
        return

    overlays = build_overlays(cfg, context)
    annotated_b64 = bu.annotate(cfg.agent, base_b64, overlays)
    if not annotated_b64:
        return

    user_message = build_user_message(cfg, context)
    agent_reply = bu.vlm_text(
        cfg.agent,
        bu.make_vlm_request(
            AGENT_SYSTEM, user_message,
            image_b64=annotated_b64,
            max_tokens=cfg.agent_max_tokens,
        ),
    )
    if not agent_reply:
        return

    parser_reply = bu.vlm_text(
        cfg.parser_agent,
        bu.make_vlm_request(
            PARSER_SYSTEM, PARSER_USER.format(raw_text=agent_reply),
            max_tokens=cfg.parser_max_tokens,
        ),
    )
    if not parser_reply:
        return

    exec_action(cfg, parser_reply, context)


# === MAIN ===

def main() -> None:
    args = bu.parse_brain_args(sys.argv[1:])
    cfg = TaskConfig(region=args.region, scale=args.scale)
    context: dict[str, Any] = {}

    while True:
        run_step(cfg, context)
        time.sleep(cfg.post_action_delay)


# === GEOMETRY HELPERS ===

def _uci_to_norm(square: str, grid_size: int = 8) -> tuple[int, int]:
    col = ord(square[0]) - ord('a')
    row = int(square[1]) - 1
    step = bu.SHARED.norm // grid_size
    x = col * step + step // 2
    y = bu.SHARED.norm - (row * step + step // 2)
    return x, y


def _make_grid_overlays(grid_size: int, color: str, stroke_width: int) -> list[dict[str, Any]]:
    overlays: list[dict[str, Any]] = []
    step = bu.SHARED.norm // grid_size
    for i in range(grid_size + 1):
        pos = i * step
        overlays.append(bu.overlay(
            points=[[pos, 0], [pos, bu.SHARED.norm]], stroke=color, stroke_width=stroke_width))
        overlays.append(bu.overlay(
            points=[[0, pos], [bu.SHARED.norm, pos]], stroke=color, stroke_width=stroke_width))
    return overlays


def _make_arrow_overlay(
    from_sq: str, to_sq: str,
    color: str, grid_size: int, stroke_width: int = 3,
) -> list[dict[str, Any]]:
    if not from_sq or not to_sq:
        return []
    step = bu.SHARED.norm // grid_size
    fx, fy = _uci_to_norm(from_sq, grid_size)
    tx, ty = _uci_to_norm(to_sq, grid_size)
    dx, dy = tx - fx, ty - fy
    length = math.hypot(dx, dy)
    if length == 0:
        return []
    ux, uy = dx / length, dy / length
    head_len = step * 0.55
    head_width = step * 0.32
    shaft_tip_x = tx - ux * head_len
    shaft_tip_y = ty - uy * head_len
    px, py = -uy, ux
    w1x = round(shaft_tip_x + px * head_width)
    w1y = round(shaft_tip_y + py * head_width)
    w2x = round(shaft_tip_x - px * head_width)
    w2y = round(shaft_tip_y - py * head_width)
    return [
        bu.overlay(
            points=[[round(fx), round(fy)], [round(shaft_tip_x), round(shaft_tip_y)]],
            stroke=color, stroke_width=stroke_width),
        bu.overlay(
            points=[[round(tx), round(ty)], [w1x, w1y], [w2x, w2y]],
            closed=True, fill=color, stroke=color, stroke_width=1),
        bu.overlay(
            points=[[round(fx), round(fy)]], stroke=color, stroke_width=1,
            label=from_sq.upper()),
        bu.overlay(
            points=[[round(tx), round(ty)]], stroke=color, stroke_width=1,
            label=to_sq.upper()),
        bu.overlay(
            points=[[bu.SHARED.norm // 2, 15]], stroke=color, stroke_width=1,
            label="PREVIOUS MOVE \u2014 DO NOT REPEAT UNLESS STRICTLY NECESSARY"),
    ]


if __name__ == "__main__":
    main()
