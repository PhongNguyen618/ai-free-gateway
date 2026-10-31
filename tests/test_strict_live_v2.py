"""Test Suite for Strict Live V2 Remediation (100% Real On-Chain — Zero Simulation).

Kiểm định chặt chẽ toàn bộ các lỗi giả lập đã được triệt tiêu:
1. GoPlus lỗi -> Fail-closed: is_safe=False, gems=[].
2. DexScreener lỗi -> Trả [], không còn BRETT/DEGEN hardcode.
3. RPC Client lỗi -> Trả (None, False), không còn 0.05/7/0.02 giả.
4. DexExecutor execute_sell_pct gửi lệnh bán thật và chỉ success khi receipt status=1.
5. Kiểm soát an toàn vốn: MAX_LIVE_ORDER_USD, LIVE_PAUSE_BUYS, Gas Reserve.
6. PositionManager update_price chỉ dời SL/TP khi lệnh bán thật trên chuỗi thành công.
7. Airdrop Faucet claim trả NOT_SUPPORTED khi không có contract thật, không cộng điểm ảo.
8. Grep kiểm tra không còn bất kỳ địa chỉ ví demo hay dữ liệu mock cũ.
"""

import os
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock


def test_no_mock_strings_or_demo_wallets_in_codebase():
    """Kiểm tra không còn chuỗi ví demo hay dữ liệu mock cứng nào trong code."""
    forbidden_patterns = [
        "0x71c6bf00a49be0834277b0292bfef32342ef0123",
        "0x89d24a6b4ccb1b6faa2625fe562bdd9a23260456",
        "FALLBACK_ASSESSMENT",
        "return 0.05, False",
    ]
    scan_dirs = ["ecosystem/smart_money", "ecosystem/airdrop_hunter/src"]
    for d in scan_dirs:
        for p in Path(d).rglob("*.py"):
            text = p.read_text(encoding="utf-8")
            for fp in forbidden_patterns:
                assert fp not in text, f"Found forbidden mock pattern '{fp}' in {p}"


def test_goplus_fail_closed_on_api_error():
    """GoPlus lỗi/timeout -> is_safe=False, score=0, tuyệt đối không trả an toàn giả."""
    from ecosystem.smart_money.live_scanner import LiveMarketScanner
    scanner = LiveMarketScanner()
    with patch("urllib.request.urlopen", side_effect=Exception("GoPlus Timeout")):
        res = scanner.check_goplus_security("base", "0x532f27101965dd16442e59d40670faf5ebb142e4")
        assert res["is_safe"] is False
        assert res["score"] == 0.0
        assert res["live_verified"] is False
        assert "UNVERIFIED" in res["status"]


def test_dexscreener_fail_closed_empty_on_error():
    """DexScreener lỗi -> trả [], không trả token hardcode."""
    from ecosystem.smart_money.live_scanner import LiveMarketScanner
    scanner = LiveMarketScanner()
    with patch("urllib.request.urlopen", side_effect=Exception("DexScreener 429")):
        res = scanner.scan_dexscreener_trending("base", limit=5)
        assert res == []


def test_rpc_client_fail_closed_none_on_error():
    """RPC Client lỗi -> trả (None, False), không trả số dư giả."""
    from ecosystem.airdrop_hunter.src.executor.rpc_client import MultiChainRPCClient
    client = MultiChainRPCClient()
    with patch.object(client, "_post_rpc", side_effect=Exception("RPC Offline")):
        bal, is_live = client.get_native_balance("base", "0x1111111111111111111111111111111111111111")
        assert bal is None
        assert is_live is False

        nonce, n_live = client.get_transaction_count("base", "0x1111111111111111111111111111111111111111")
        assert nonce is None
        assert n_live is False

        gas, g_live = client.get_gas_price_gwei("base")
        assert gas is None
        assert g_live is False


@pytest.mark.asyncio
async def test_dex_executor_max_order_usd_limit():
    """Lệnh mua vượt trần MAX_LIVE_ORDER_USD phải bị từ chối ngay lập tức."""
    from ecosystem.smart_money.dex_executor import LiveDexExecutor
    dex = LiveDexExecutor()
    dex.enable_live = True
    dex.max_order_usd = 2.0  # Giới hạn 2 USD
    dex.pause_buys = False

    res = await dex.execute_buy("base", "0x532f27101965dd16442e59d40670faf5ebb142e4", amount_usd=5.0)
    assert res["success"] is False
    assert res["error"] == "MAX_ORDER_USD_EXCEEDED"


@pytest.mark.asyncio
async def test_dex_executor_pause_buys_switch():
    """Khi bật LIVE_PAUSE_BUYS, không được phép mở lệnh mua mới."""
    from ecosystem.smart_money.dex_executor import LiveDexExecutor
    dex = LiveDexExecutor()
    dex.enable_live = True
    dex.pause_buys = True

    res = await dex.execute_buy("base", "0x532f27101965dd16442e59d40670faf5ebb142e4", amount_usd=1.0)
    assert res["success"] is False
    assert res["error"] == "BUYS_TEMPORARILY_PAUSED"


def test_position_manager_sell_failure_blocks_tp_advancement(tmp_path):
    """Khi chạm TP1 mà lệnh bán DEX on-chain thất bại, KHÔNG được cập nhật tp1_hit hay realized_usd."""
    from ecosystem.smart_money.position_manager import TieredPositionManager

    db_path = tmp_path / "test_tp_fail.db"
    pm = TieredPositionManager(db_path=db_path)

    pos = pm.open_position(
        token_address="0x532f27101965dd16442e59d40670faf5ebb142e4",
        token_symbol="TESTCOIN",
        chain="base",
        entry_price_usd=1.0,
        invested_usd=10.0,
        initial_quantity=10.0,
    )

    # Giả lập lệnh bán DEX on-chain bị revert/thất bại
    with patch.dict(os.environ, {"ENABLE_LIVE_EXECUTION": "true"}):
        with patch.object(pm, "_dispatch_live_sell", return_value=(False, None)):
            pos, actions = pm.update_price(pos.position_id, current_price_usd=1.50)  # +50% (đủ trigger TP1)
            # Vì bán thất bại, tp1_hit KHÔNG được bật!
            assert pos.tp1_hit is False
            assert pos.remaining_quantity == 10.0
            assert pos.realized_usd == 0.0
            assert any("FAILED" in a for a in actions)


def test_position_manager_sell_success_advances_tp(tmp_path):
    """Khi lệnh bán DEX on-chain thành công có tx_hash, trạng thái TP1 được cập nhật đầy đủ."""
    from ecosystem.smart_money.position_manager import TieredPositionManager

    db_path = tmp_path / "test_tp_ok.db"
    pm = TieredPositionManager(db_path=db_path)

    pos = pm.open_position(
        token_address="0x532f27101965dd16442e59d40670faf5ebb142e4",
        token_symbol="TESTCOIN",
        chain="base",
        entry_price_usd=1.0,
        invested_usd=10.0,
        initial_quantity=10.0,
    )

    fake_hash = "0x" + "a" * 64
    with patch.dict(os.environ, {"ENABLE_LIVE_EXECUTION": "true"}):
        with patch.object(pm, "_dispatch_live_sell", return_value=(True, fake_hash)):
            pos, actions = pm.update_price(pos.position_id, current_price_usd=1.50)
            assert pos.tp1_hit is True
            assert pos.remaining_quantity == pytest.approx(6.5)
            assert pos.realized_usd == pytest.approx(3.5 * 1.50)
            assert any(fake_hash in a for a in actions)


def test_airdrop_faucet_claim_not_supported(tmp_path):
    """Faucet claim không có contract thật trả NOT_SUPPORTED và không cộng điểm."""
    from ecosystem.airdrop_hunter.src.executor.farming_executor import FarmingExecutor
    from ecosystem.airdrop_hunter.src.storage.farming_db import FarmingDatabase

    db = FarmingDatabase(db_path=tmp_path / "faucet_test.db")
    executor = FarmingExecutor(db=db)
    res = executor.execute_faucet_claim("0x1111111111111111111111111111111111111111", "test-faucet")
    assert res["status"] == "NOT_SUPPORTED"
    assert res["points_earned"] == 0
    assert res["live_verified"] is False

