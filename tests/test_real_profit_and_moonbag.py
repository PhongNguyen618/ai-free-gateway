"""Tests for Real Profit, Tiered Scaling-Out, Moonbag x50 Engine, and On-Chain Tools."""

from __future__ import annotations

import pytest
from pathlib import Path

from ecosystem.smart_money.position_manager import TieredPositionManager, PositionState
from ecosystem.smart_money.live_scanner import LiveMarketScanner
from ecosystem.airdrop_hunter.src.executor.onchain_signer import BaseWethSigner, DefiYieldScanner


def test_tiered_position_manager_4_stage_scaling_out_to_moonbag_50x(tmp_path: Path):
    """Verifies quant 4-stage scaling out: TP1 (+40%), TP2 (X2), TP3 (X4), and Moonbag 50X."""
    db_file = tmp_path / "test_positions.db"
    pm = TieredPositionManager(db_path=db_file)

    # 1. Open a $100 position in $BRETT at $0.10 (Quantity = 1000 BRETT)
    pos = pm.open_position(
        token_address="0x532f27101965dd16442e59d40670faf5ebb142e4",
        token_symbol="BRETT",
        chain="base",
        entry_price_usd=0.10,
        invested_usd=100.0,
    )
    assert pos.status == "OPEN"
    assert pos.initial_quantity == 1000.0
    assert pos.remaining_quantity == 1000.0
    assert pos.remaining_pct == 1.0
    assert pos.stop_loss_price_usd == pytest.approx(0.082)  # -18% Hard SL

    # 2. Price rises to $0.145 (+45% / 1.45x) -> Triggers TP1
    pos, actions = pm.update_price(pos.position_id, 0.145)
    assert pos.tp1_hit is True
    assert pos.remaining_quantity == pytest.approx(650.0)  # Sold 35% (350 tokens)
    assert pos.remaining_pct == pytest.approx(0.65)
    assert pos.realized_usd == pytest.approx(350.0 * 0.145)  # $50.75 cash realized
    assert pos.stop_loss_price_usd == pytest.approx(0.102)  # Moved to Breakeven (+2%)
    assert any("TP1" in a for a in actions)

    # 3. Price rises to $0.21 (+110% / 2.1x) -> Triggers TP2 (Free Ride!)
    pos, actions = pm.update_price(pos.position_id, 0.21)
    assert pos.tp2_hit is True
    assert pos.remaining_quantity == pytest.approx(350.0)  # Sold 30% (300 tokens)
    assert pos.remaining_pct == pytest.approx(0.35)
    # Total realized cash now exceeds initial invested $100! (50.75 + 300*0.21 = 113.75)
    assert pos.realized_usd > 100.0
    assert pos.stop_loss_price_usd == pytest.approx(0.14)  # Trailing stop moved to +40%
    assert any("FREE RIDE" in a for a in actions)

    # 4. Price rises to $0.42 (+320% / 4.2x) -> Triggers TP3 & Activates Moonbag
    pos, actions = pm.update_price(pos.position_id, 0.42)
    assert pos.tp3_hit is True
    assert pos.status == "MOONBAG"  # Switched to Moonbag mode
    assert pos.remaining_quantity == pytest.approx(200.0)  # 20% initial qty left
    assert pos.remaining_pct == pytest.approx(0.20)
    assert pos.stop_loss_price_usd == pytest.approx(0.42 * 0.65)  # -35% from ATH
    assert any("MOONBAG" in a for a in actions)

    # 5. Moonbag hits 10X ($1.05 / 10.5x)
    pos, actions = pm.update_price(pos.position_id, 1.05)
    assert pos.moonbag_10x_hit is True
    assert pos.remaining_quantity == pytest.approx(150.0)  # Sold 5% at 10x
    assert pos.remaining_pct == pytest.approx(0.15)
    assert any("10X" in a for a in actions)

    # 6. Moonbag hits 50X ($5.20 / 52x) -> Jackpot reached!
    pos, actions = pm.update_price(pos.position_id, 5.20)
    assert pos.moonbag_50x_hit is True
    assert pos.status == "CLOSED"
    assert pos.remaining_quantity == 0.0
    # Realized cash: 50.75 + 63.0 + 63.0 + 52.5 + (150 * 5.20 = 780.0) = > $1000!
    assert pos.realized_usd > 1000.0
    assert pos.total_return_usd > 900.0
    assert any("50X" in a for a in actions)


def test_tiered_position_breakeven_protection(tmp_path: Path):
    """Verifies that after TP1, if market dumps back to entry, trade exits in net profit."""
    db_file = tmp_path / "test_breakeven.db"
    pm = TieredPositionManager(db_path=db_file)

    pos = pm.open_position(
        token_address="0x4ed4e862860bed51a9570b96d89af5e1b0efefed",
        token_symbol="DEGEN",
        chain="base",
        entry_price_usd=0.01,
        invested_usd=100.0,
    )

    # Hits TP1 at $0.0145 (+45%)
    pos, _ = pm.update_price(pos.position_id, 0.0145)
    assert pos.tp1_hit is True
    assert pos.stop_loss_price_usd == pytest.approx(0.0102)

    # Dumps down to $0.0101 (below breakeven stop loss)
    pos, actions = pm.update_price(pos.position_id, 0.0101)
    assert pos.status == "CLOSED"
    assert pos.close_reason == "STOP_LOSS_HIT"
    # Even after dump, total return is POSITIVE because 35% was locked at +45%!
    assert pos.total_return_usd > 0.0


def test_live_market_scanner_and_goplus_security():
    """Verifies live scanner fetches candidates and GoPlus filters honeypots."""
    scanner = LiveMarketScanner()
    gems = scanner.get_safe_trending_gems(chain="base", limit=2)
    assert isinstance(gems, list)
    assert len(gems) > 0
    top = gems[0]
    assert "token_address" in top
    assert "symbol" in top
    assert top["is_safe"] is True
    assert top["security_score"] >= 80.0


def test_base_weth_signer_and_yield_scanner():
    """Verifies canonical Base WETH zero-fee cycle and DefiLlama yield scanner."""
    signer = BaseWethSigner()
    res = signer.execute_weth_cycle(
        wallet_address="0x46babd83588f736cdfad09197e6ba4b40b132728",
        action="wrap",
        amount_eth=0.0001,
    )
    assert res["status"] in ("LIVE_ONCHAIN_CONFIRMED", "FAILED_NO_PRIVATE_KEY", "SIMULATION_VERIFIED", "GAS_CEILING_EXCEEDED")
    assert res["contract"] == "0x4200000000000000000000000000000000000006"
    if res["status"] == "LIVE_ONCHAIN_CONFIRMED":
        assert "tx_hash" in res


    yield_scanner = DefiYieldScanner()
    pools = yield_scanner.scan_base_yield_pools(limit=3)
    assert isinstance(pools, list)
    assert len(pools) > 0
    p0 = pools[0]
    assert "protocol" in p0
    assert "apy_total_pct" in p0
    assert p0["apy_total_pct"] > 0.0
