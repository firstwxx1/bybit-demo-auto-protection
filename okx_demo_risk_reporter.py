"""Deterministic, credential-free core for read-only OKX risk reports."""
from __future__ import annotations

import argparse
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ValidationError(ValueError):
    pass


def number(value: Any, name: str, *, positive: bool = False) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{name}必须为数值") from exc
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValidationError(f"{name}必须为有限正数")
    return result


@dataclass(frozen=True)
class Position:
    instrument: str
    side: str
    size: float
    leverage: float
    entry_price: float
    mark_price: float
    unrealized_pnl: float
    liquidation_price: float | None
    margin_mode: str | None = None
    position_side: str | None = None
    size_unit: str = "张"


def position_from_snapshot(raw: dict[str, Any]) -> Position:
    side = str(raw.get("side", "")).lower()
    if side not in {"long", "short"}:
        raise ValidationError("side必须是long或short")
    instrument = str(raw.get("instrument", "")).strip()
    if not instrument.endswith("-SWAP"):
        raise ValidationError("仅支持OKX永续合约持仓")
    margin_mode = raw.get("margin_mode")
    if margin_mode not in (None, "cross", "isolated"):
        raise ValidationError("margin_mode必须是cross或isolated")
    position_side = raw.get("position_side")
    if position_side not in (None, "net", "long", "short"):
        raise ValidationError("position_side必须是net、long或short")
    if position_side not in (None, "net", side):
        raise ValidationError("position_side与持仓方向不一致")
    return Position(
        instrument=instrument,
        side=side,
        size=number(raw.get("size"), "size", positive=True),
        leverage=number(raw.get("leverage"), "leverage", positive=True),
        entry_price=number(raw.get("entry_price"), "entry_price", positive=True),
        mark_price=number(raw.get("mark_price"), "mark_price", positive=True),
        unrealized_pnl=number(raw.get("unrealized_pnl", 0), "unrealized_pnl"),
        liquidation_price=(number(raw["liquidation_price"], "liquidation_price", positive=True)
                           if raw.get("liquidation_price") not in (None, "") else None),
        margin_mode=margin_mode,
        position_side=position_side,
        size_unit=str(raw.get("size_unit") or "张"),
    )


def fixed_stop(position: Position, entry_pct: float) -> float:
    pct = number(entry_pct, "entry_pct", positive=True)
    if pct > 0.25:
        raise ValidationError("固定止损比例不得超过25%")
    factor = 1 + pct if position.side == "short" else 1 - pct
    return round(position.entry_price * factor, 4)


def validate_stop(position: Position, stop: float) -> tuple[bool, str]:
    if position.side == "short":
        if stop <= position.mark_price:
            return False, "空单止损必须高于当前标记价"
        if position.liquidation_price and stop >= position.liquidation_price:
            return False, "空单止损不得越过强平价"
    else:
        if stop >= position.mark_price:
            return False, "多单止损必须低于当前标记价"
        if position.liquidation_price and stop <= position.liquidation_price:
            return False, "多单止损不得越过强平价"
    return True, "通过（固定程序兜底）"


def fib_targets(position: Position, candles: list[dict[str, Any]]) -> dict[str, Any]:
    if not candles:
        return {"valid": False, "reason": "4H摆动数据不足"}
    # Use only the newest local structure; distant extremes distort targets.
    local_candles = candles[:20]
    highs = [number(row.get("high"), "high", positive=True) for row in local_candles]
    lows = [number(row.get("low"), "low", positive=True) for row in local_candles]
    high, low = max(highs), min(lows)
    if high <= low:
        return {"valid": False, "reason": "4H摆动区间无效"}
    width = high - low
    if position.side == "short":
        levels = (low - width * 0.272, low - width * 0.618)
        broken = position.mark_price > high
    else:
        levels = (high + width * 0.272, high + width * 0.618)
        broken = position.mark_price < low
    result = {"timeframe": "4H（局部摆动点）", "high": high, "low": low,
              "window_candles": len(local_candles),
              "fib_0618_retracement": round(
                  low + width * 0.618 if position.side == "short" else high - width * 0.618, 4
              ),
              "fib_1272": round(levels[0], 4), "fib_1618": round(levels[1], 4)}
    if broken:
        return result | {"valid": False, "reason": "Fib结构已被当前价格突破，止盈失效"}
    return result | {"valid": True, "reason": "结构有效"}


def liquidation_distance(position: Position) -> float | None:
    if position.liquidation_price is None:
        return None
    return abs(position.liquidation_price / position.mark_price - 1) * 100


def moving_averages(snapshot: dict[str, Any]) -> dict[str, float | None]:
    closes = []
    for row in snapshot.get("candles", []):
        try:
            close = float(row.get("close"))
        except (AttributeError, TypeError, ValueError):
            continue
        if math.isfinite(close) and close > 0:
            closes.append(close)
    result: dict[str, float | None] = {}
    for period in (5, 10, 20, 60):
        result[f"MA{period}"] = round(sum(closes[:period]) / period, 4) if len(closes) >= period else None
    return result


def average_range_pct(snapshot: dict[str, Any], limit: int = 20) -> float | None:
    ranges = []
    for row in snapshot.get("candles", [])[:limit]:
        try:
            high = number(row.get("high"), "high", positive=True)
            low = number(row.get("low"), "low", positive=True)
            close = number(row.get("close"), "close", positive=True)
        except (AttributeError, ValidationError):
            continue
        if high >= low:
            ranges.append((high - low) / close)
    return sum(ranges) / len(ranges) if ranges else None


def build_report(snapshot: dict[str, Any], *, stop_pct: float | None = None) -> str:
    pos = position_from_snapshot(snapshot["position"])
    pct = stop_pct if stop_pct is not None else float(os.getenv("FIXED_STOP_ENTRY_PCT", "0.04855847842644323"))
    stop = fixed_stop(pos, pct)
    effective_stop = stop
    stop_adjusted = False
    stop_adjustment_reason = ""
    stop_boundary = None
    boundary_safe = True
    stop_reason = ""
    model_stop_used = False
    # If GPT provides a valid stop_loss, use it as the initial candidate instead of fixed stop.
    model = snapshot.get("risk_model", {})
    model_ok = model.get("available") is True
    if model_ok and model.get("stop_loss") not in (None, ""):
        try:
            model_stop = number(model["stop_loss"], "model_stop_loss", positive=True)
            model_stop_valid, model_stop_reason = validate_stop(pos, model_stop)
            if model_stop_valid:
                effective_stop = model_stop
                model_stop_used = True
        except (TypeError, ValueError, ValidationError):
            pass
    if pos.liquidation_price is not None:
        buffer_pct = float(os.getenv("LIQUIDATION_BUFFER_PCT", "0.001"))
        stop_boundary = round(pos.liquidation_price * (1 - buffer_pct if pos.side == "short" else 1 + buffer_pct), 4)
        boundary_safe = stop_boundary > pos.mark_price if pos.side == "short" else stop_boundary < pos.mark_price
        if not boundary_safe:
            stop_ok = False
            stop_reason = "空单止损不得越过强平价；当前价已接近或越过强平安全边界" if pos.side == "short" else "多单止损不得越过强平价；当前价已接近或越过强平安全边界"
        elif pos.side == "short" and stop >= stop_boundary:
            effective_stop = stop_boundary
            stop_adjusted = True
            stop_adjustment_reason = "强平缓冲边界"
        elif pos.side == "long" and stop <= stop_boundary:
            effective_stop = stop_boundary
            stop_adjusted = True
            stop_adjustment_reason = "强平缓冲边界"
    stop_ok, stop_reason = validate_stop(pos, effective_stop) if stop_boundary is None or boundary_safe else (False, stop_reason)
    configured_max_stop_distance = float(os.getenv("FIXED_STOP_MAX_MARK_DISTANCE_PCT", "0.015"))
    if not math.isfinite(configured_max_stop_distance) or configured_max_stop_distance <= 0:
        raise ValidationError("固定止损距离上限必须为有限正数")
    leverage_stop_distance = 0.25 / pos.leverage if pos.leverage >= 20 else configured_max_stop_distance
    max_stop_distance = min(configured_max_stop_distance, leverage_stop_distance)
    distance_boundary = round(
        pos.mark_price * (1 + max_stop_distance if pos.side == "short" else 1 - max_stop_distance), 4
    )
    if boundary_safe and ((pos.side == "short" and effective_stop > distance_boundary) or (pos.side == "long" and effective_stop < distance_boundary)):
        effective_stop = distance_boundary
        stop_adjusted = True
        stop_adjustment_reason = (
            f"杠杆风险上限{max_stop_distance * 100:g}%"
            if max_stop_distance < configured_max_stop_distance
            else f"原候选距离现价超过{configured_max_stop_distance * 100:g}%"
        )
        stop_ok, stop_reason = validate_stop(pos, effective_stop)
    fib = snapshot.get("fib")
    if not isinstance(fib, dict):
        fib = fib_targets(pos, snapshot.get("fib_candles", snapshot.get("candles", [])))
    fib_close_candidate = snapshot.get("fib_close_candidate", {})
    fib_ok = fib.get("valid") is True
    fib_lock_applied = False
    price_profitable = pos.mark_price < pos.entry_price if pos.side == "short" else pos.mark_price > pos.entry_price
    if fib_ok and price_profitable:
        fib_lock_value = fib.get("fib_0618_retracement")
        if fib_lock_value in (None, "") and fib.get("high") is not None and fib.get("low") is not None:
            high = number(fib["high"], "fib_high", positive=True)
            low = number(fib["low"], "fib_low", positive=True)
            width = high - low
            fib_lock_value = low + width * 0.618 if pos.side == "short" else high - width * 0.618
        if fib_lock_value not in (None, ""):
            fib_lock = number(fib_lock_value, "fib_0618_retracement", positive=True)
            fib = fib | {"fib_0618_retracement": round(fib_lock, 4)}
            tighter = min(effective_stop, fib_lock) if pos.side == "short" else max(effective_stop, fib_lock)
            if tighter != effective_stop and validate_stop(pos, tighter)[0]:
                effective_stop = round(tighter, 4)
                stop_adjusted = True
                stop_adjustment_reason = "Fib 0.618盈利回撤保护"
                fib_lock_applied = True
                stop_ok, stop_reason = validate_stop(pos, effective_stop)
    grok = snapshot.get("grok", {})
    news_verified = grok.get("evidence_verified") is True
    short_term_news_gate = pos.leverage >= 20 and not news_verified
    model_take_profit = model.get("take_profit") if model_ok and not short_term_news_gate else None
    take_profit = model_take_profit
    tp_source = "GPT仓位分析"
    atr_pct = average_range_pct(snapshot)
    fixed_fib_fallback = take_profit in (None, "")
    near_target_cap_pct = float(os.getenv("FIXED_TP_MAX_DISTANCE_PCT", "0.03"))
    if fixed_fib_fallback and stop_ok:
        risk_distance = abs(effective_stop - pos.mark_price)
        if short_term_news_gate:
            target_r = 1.0
        elif atr_pct is None:
            target_r = 1.0
        elif atr_pct <= 0.001:
            target_r = 1.0
        elif atr_pct <= 0.0015:
            target_r = 1.25
        elif atr_pct <= 0.0025:
            target_r = 1.5
        else:
            target_r = 1.5
        near_target = (pos.mark_price - target_r * risk_distance
                       if pos.side == "short" else pos.mark_price + target_r * risk_distance)
        direction_boundary = (
            min(pos.mark_price, pos.entry_price) * (1 - 1e-6)
            if pos.side == "short"
            else max(pos.mark_price, pos.entry_price) * (1 + 1e-6)
        )
        near_target = min(near_target, direction_boundary) if pos.side == "short" else max(near_target, direction_boundary)
        max_distance = pos.mark_price * near_target_cap_pct
        capped_target = (pos.mark_price - max_distance
                         if pos.side == "short" else pos.mark_price + max_distance)
        capped_target = (
            min(max(near_target, capped_target), direction_boundary)
            if pos.side == "short"
            else max(min(near_target, capped_target), direction_boundary)
        )
        take_profit = round(capped_target, 4)
        tp_source = f"固定程序ATR近端{target_r:g}R候选" + ("（Fib回撤保护后）" if fib_lock_applied else "")
    elif fixed_fib_fallback:
        take_profit = fib.get("fib_1272")
        tp_source = "固定程序Fib 1.272候选"
    tp_ok = False
    risk_reward = None
    entry_risk_reward = None
    tp_reasons: list[str] = []
    if take_profit not in (None, ""):
        try:
            tp = number(take_profit, "take_profit", positive=True)
            direction_ok = tp < min(pos.mark_price, pos.entry_price) if pos.side == "short" else tp > max(pos.mark_price, pos.entry_price)
            if tp_source.startswith(("多因子近端", "固定程序ATR近端")):
                fib_range_ok = True
            else:
                fib_levels = (float(fib["fib_1272"]), float(fib["fib_1618"]))
                fib_range_ok = min(fib_levels) <= tp <= max(fib_levels)
            local_distance_ok = not fixed_fib_fallback or abs(tp - pos.mark_price) / pos.mark_price <= 0.10
            risk = abs(effective_stop - pos.mark_price)
            reward = abs(pos.mark_price - tp)
            risk_reward = round(reward / risk, 4) if risk > 0 and stop_ok else None
            entry_risk = abs(effective_stop - pos.entry_price)
            entry_reward = abs(tp - pos.entry_price)
            entry_risk_reward = round(entry_reward / entry_risk, 4) if entry_risk > 0 and stop_ok else None
            tp_ok = direction_ok and fib_range_ok and local_distance_ok and stop_ok and risk_reward is not None and risk_reward >= 1.0
            if not direction_ok:
                tp_reasons = ["止盈方向无效"]
            elif not fib_range_ok:
                tp_reasons = ["止盈价不在Fib有效区间"]
            elif not local_distance_ok:
                tp_reasons = ["固定Fib止盈距离超过10%"]
            elif not stop_ok:
                tp_reasons = ["止损校验未通过"]
            elif risk_reward is None or risk_reward < 1.0:
                tp_reasons = [f"风险收益比不足：{risk_reward:g}" if risk_reward is not None else "风险收益比无效"]
            elif tp_source == "固定程序Fib 1.272候选":
                tp_reasons = ["通过（固定程序Fib 1.272候选）"]
            elif tp_source.startswith(("多因子近端", "固定程序ATR近端")):
                tp_reasons = [f"通过（{tp_source}）"]
        except (KeyError, TypeError, ValueError):
            tp_reasons = ["止盈价无效"]
    independent_model = snapshot.get("independent_risk_model", {})
    independent_model_ok = independent_model.get("available") is True
    model_reason = "" if model_ok else str(model.get("error", "GPT模型不可用"))
    if not model_ok:
        model_status = f"不可用：{model_reason}"
    else:
        model_status = f"可用：{model.get('model', 'gpt-5.6-sol')}"
    if not independent_model_ok:
        independent_status = f"不可用：{independent_model.get('error', '独立风险来源不可用')}"
    else:
        independent_status = f"可用：{independent_model.get('model', 'independent-risk')}"
    decision_reason = snapshot.get("decision", {}).get("reason", "未提供")
    if not tp_ok:
        if not model_ok:
            tp_reasons.append("GPT本轮无有效输出")
        tp_reasons.append("止盈价无效")
    if not fib_ok and not tp_source.startswith(("多因子近端", "固定程序ATR近端")):
        tp_reasons.append(fib.get("reason", "Fib无效"))
    tp_reason = "；".join(dict.fromkeys(tp_reasons)) or "通过"
    stop_source = "GPT模型建议" if model_stop_used else "固定程序兜底"
    if fib_lock_applied:
        stop_source += " + Fib 0.618盈利回撤保护"
    stop_explanation = []
    if stop_ok:
        if pos.side == "short":
            if effective_stop > pos.mark_price:
                stop_explanation.append(f"止损高于当前价{effective_stop - pos.mark_price:.2f}点，反弹触发")
            if effective_stop < pos.entry_price:
                locked_pct = (pos.entry_price - effective_stop) / pos.entry_price * 100
                stop_explanation.append(f"止损低于开仓价{pos.entry_price - effective_stop:.2f}点，触发后仍锁定约{locked_pct:.2f}%价格利润")
        else:
            if effective_stop < pos.mark_price:
                stop_explanation.append(f"止损低于当前价{pos.mark_price - effective_stop:.2f}点，回落触发")
            if effective_stop > pos.entry_price:
                locked_pct = (effective_stop - pos.entry_price) / pos.entry_price * 100
                stop_explanation.append(f"止损高于开仓价{effective_stop - pos.entry_price:.2f}点，触发后仍锁定约{locked_pct:.2f}%价格利润")
    stop_logic = (
        f"止损逻辑：{'空单盈利保护' if pos.side == 'short' and pos.mark_price < pos.entry_price else '多单盈利保护' if pos.side == 'long' and pos.mark_price > pos.entry_price else '固定风险保护'}"
        + ("；" + "；".join(stop_explanation) if stop_explanation else "")
    )
    cached_at = snapshot.get("cached_at")
    source = f"最近成功缓存（{cached_at}，可能过期）" if cached_at else "OKX实时查询"
    liq = "未返回" if pos.liquidation_price is None else f"{pos.liquidation_price:g}"
    liq_distance = liquidation_distance(pos)
    fib_status = fib.get("reason", "未知")
    atr_pct = average_range_pct(snapshot)
    fib_lines = [
        "Fib参考：", f"周期：{fib.get('timeframe', '4H（局部摆动点）')}",
        f"摆动高点：{fib.get('high', 'N/A')}", f"摆动低点：{fib.get('low', 'N/A')}",
        f"Fib 0.618回撤保护：{fib.get('fib_0618_retracement', 'N/A')}",
        f"Fib 1.272：{fib.get('fib_1272', 'N/A')}", f"Fib 1.618：{fib.get('fib_1618', 'N/A')}",
        f"状态：{'有效' if fib_ok else '已失效'}，{fib_status}",
    ]
    risks = [
        "1. 当前持仓处于浮亏方向，固定风控继续运行。" if pos.unrealized_pnl < 0 else "1. 当前持仓未处于浮亏，固定风控继续运行。",
        f"2. 当前杠杆为{pos.leverage:g}倍，价格反向波动会放大损失。",
        (f"3. 当前标记价距离强平价约{liq_distance:.2f}%，强平价会随账户状态变化。"
         if liq_distance is not None else "3. OKX未返回强平价，无法计算强平距离。"),
        f"4. 当前未实现盈亏为{pos.unrealized_pnl:g} USDT。",
    ]
    return "\n".join([
        "**【量化风险报告】**",
        f"时间：{snapshot.get('timestamp', datetime.now(timezone.utc).isoformat())}",
        f"**持仓：{pos.instrument}，{'多单' if pos.side == 'long' else '空单'}，数量={pos.size:g}{pos.size_unit}（OKX合约张数，非BTC数量），杠杆={pos.leverage:g}x，开仓均价={pos.entry_price:g}，标记价={pos.mark_price:g}，未实现盈亏={pos.unrealized_pnl:g} USDT，强平价={liq}**",
        f"**风险等级：{model.get('risk_level', '模型不可用（固定风控运行中）') if model_ok else '模型不可用（固定风控运行中）'}**",
        f"置信度：{model.get('confidence', 0) if model_ok else 0}",
        f"GPT状态：{model_status}",
        f"**独立风险源状态：{independent_status}**",
        f"GPT建议：{model.get('recommendation', '仅观察') if model_ok else '无有效输出'}",
        f"**确定性决策：{snapshot.get('decision', {}).get('action', '未提供')}**",
        f"决策原因：{decision_reason}",
        f"**建议：{model.get('recommendation', '仅观察') if model_ok else model_reason + '，保留固定止损保护；不采用GPT止盈建议，固定程序候选仅供人工参考；人工复核'}**",
        f"**止盈建议：{take_profit if tp_ok else '暂不采用'}；{tp_reason}**",
        f"止盈来源：{tp_source if tp_ok else '无有效候选'}",
        f"止损候选价：{stop:g}",
        f"止损允许{'上限' if pos.side == 'short' else '下限'}：{stop_boundary:g}" if stop_boundary is not None else "止损允许边界：无强平价数据",
        f"**{stop_logic}**",
        (f"**安全修正止损价：{effective_stop:g}（{stop_adjustment_reason or '风险边界修正'}）**" if stop_adjusted
         else (f"**止损保护建议：{effective_stop:g}**" if stop_ok
               else f"止损保护告警：{stop_reason}；候选价={stop:g}，边界={stop_boundary if stop_boundary is not None else 'N/A'}")),
        f"**止损保护建议：{effective_stop:g}**" if stop_adjusted and stop_ok else "",
        f"**止损来源：{stop_source}**",
        *[f"MA{period}：{value if value is not None else 'N/A'}" for period, value in ((5, moving_averages(snapshot)['MA5']), (10, moving_averages(snapshot)['MA10']), (20, moving_averages(snapshot)['MA20']), (60, moving_averages(snapshot)['MA60']))],
        f"波动率参考（ATR）：{atr_pct * 100:g}%" if atr_pct is not None else "波动率参考（ATR）：数据不足",
        f"风险收益比（当前标记价基准）：{risk_reward:g}" if risk_reward is not None else (
            "风险收益比（当前标记价基准）：不适用（止损校验未通过）" if not stop_ok else "风险收益比（当前标记价基准）：不适用（止盈已失效）"
        ),
        f"风险收益比（开仓均价基准）：{entry_risk_reward:g}" if entry_risk_reward is not None else "风险收益比（开仓均价基准）：不适用",
        *fib_lines,
        f"**Fib浮动平仓候选：{fib_close_candidate.get('status', '未提供')}；阶段={fib_close_candidate.get('stage', 'N/A')}；比例={fib_close_candidate.get('close_fraction', 'N/A')}；执行={fib_close_candidate.get('execution', 'PAPER_ONLY')}**",
        "**Fib浮动平仓说明：仅模拟候选，不执行交易。**",
        "有效期：15分钟",
        f"**止损校验：{'通过' if stop_ok else '未通过'}（{stop_source}）**",
        f"**止盈校验：{'通过' if tp_ok else '未通过'}；{tp_reason}**",
        f"数据状态：{source}",
        "**新闻状态：" + (
            snapshot.get("grok", {}).get("summary")
            if snapshot.get("grok", {}).get("evidence_verified") is True
            else "新闻证据未通过校验，原始摘要不纳入报告"
        ) + "**",
        "**持仓风险：**", *risks,
        "**说明：仅根据OKX实际持仓币种分析，不显示固定BTC价格；不执行交易。**",
    ])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", required=True, type=Path)
    args = parser.parse_args()
    print(build_report(json.loads(args.fixture.read_text(encoding="utf-8"))))


if __name__ == "__main__":
    main()
