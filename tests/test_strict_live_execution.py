"""Test Suite for Strict Live On-Chain Execution (100% Real — Zero Simulation).

Kiểm tra toàn diện:
1. Thư viện mật mã trong requirements.txt.
2. Xóa bỏ hoàn toàn chế độ SIMULATION_VERIFIED và hàm _generate_mock_tx_hash.
3. Fail-closed: Nếu thiếu Key hoặc Gas, báo lỗi on-chain thật, không bao giờ sinh hash giả.
4. DexExecutor EVM/Solana & Live Wallet Balance Auditor.
5. Smart Money Hub Bridge sử dụng GoPlus & DexScreener thật.
"""

import os
import pytest
from unittest.mock import patch, MagicMock


def test_requirements_contains_crypto_dependencies():
    """Kiểm tra requirements.txt có đầy đủ các package ký giao dịch thật."""
    with open("requirements.txt", "r", encoding="utf-8") as f:
        content = f.read()

    assert "eth-account" in content
    assert "eth-utils" in content
    assert "solders" in content
    assert "base58" in content
    assert "requests" in content


def test_farming_mode_defaults_to_live():
    """Kiểm tra cấu hình farming mặc định chuyển sang LIVE."""
    from ecosystem.airdrop_hunter.src import config
    # Khi không có biến môi trường FARMING_MODE thì mặc định là LIVE
    assert config.FARMING_MODE in ("LIVE", "STRICT_LIVE")


def test_no_mock_tx_hash_method_in_farming_executor():
    """Kiểm tra class FarmingExecutor đã xóa hoàn toàn phương thức sinh hash giả."""
    from ecosystem.airdrop_hunter.src.executor.farming_executor import FarmingExecutor
    assert not hasattr(FarmingExecutor, "_generate_mock_tx_hash")


def test_weth_signer_fail_closed_without_private_key():
    """Kiểm tra BaseWethSigner từ chối sinh hash giả khi không có private key."""
    from ecosystem.airdrop_hunter.src.executor.onchain_signer import BaseWethSigner
    with patch.dict(os.environ, {"EVM_PRIVATE_KEY": ""}, clear=False):
        signer = BaseWethSigner()
        signer.private_key = ""
        res = signer.execute_weth_cycle(wallet_address="0x1111111111111111111111111111111111111111", action="wrap")

        # Phải báo FAILED_NO_PRIVATE_KEY hoặc lỗi preflight
        assert res.get("status") == "FAILED_NO_PRIVATE_KEY"
        assert res.get("live_verified") is False
        assert "tx_hash" not in res or res.get("tx_hash") is None
        # Tuyệt đối không sinh hash giả mạo
        assert res.get("status") != "SIMULATION_VERIFIED"


def test_reward_harvester_has_onchain_auditor(tmp_path):
    """Kiểm tra RewardHarvester có tính năng truy vấn số dư on-chain thật."""
    from ecosystem.airdrop_hunter.src.executor.reward_harvester import RewardHarvester
    from ecosystem.airdrop_hunter.src.storage.farming_db import FarmingDatabase

    db_path = tmp_path / "farming_test.db"
    db = FarmingDatabase(db_path=db_path)
    harvester = RewardHarvester(db=db)
    assert hasattr(harvester, "audit_real_onchain_balances")


@pytest.mark.asyncio
async def test_dex_executor_resolves_and_fail_closed():
    """Kiểm tra LiveDexExecutor xử lý an toàn khi thiếu key hoặc gas."""
    from ecosystem.smart_money.dex_executor import LiveDexExecutor
    dex = LiveDexExecutor()

    # Không có key thì trả về None
    dex.evm_private_key = ""
    dex.solana_private_key = ""
    assert dex.resolve_evm_address() is None
    assert dex.resolve_solana_address() is None

    # Khi tắt ENABLE_LIVE_EXECUTION, execute_buy từ chối
    dex.enable_live = False
    res = await dex.execute_buy(chain="base", token_address="0x4200000000000000000000000000000000000006", amount_usd=5.0)
    assert res.get("success") is False
    assert res.get("error") == "LIVE_EXECUTION_PAUSED"
    assert res.get("live_verified") is False


def test_position_manager_fail_closed_on_live_execution(tmp_path):
    """Kiểm tra TieredPositionManager từ chối mở vị thế ảo khi bật execute_onchain mà thiếu gas."""
    from ecosystem.smart_money.position_manager import TieredPositionManager

    db_file = tmp_path / "positions_test.db"
    pm = TieredPositionManager(db_path=db_file)
    # Bật ENABLE_LIVE_EXECUTION=true nhưng không có key/gas
    with patch.dict(os.environ, {"ENABLE_LIVE_EXECUTION": "true", "EVM_PRIVATE_KEY": ""}):
        with pytest.raises(RuntimeError) as exc_info:
            pm.open_position(
                token_address="0x4200000000000000000000000000000000000006",
                token_symbol="TESTWETH",
                chain="base",
                entry_price_usd=2500.0,
                invested_usd=10.0,
                execute_onchain=True,
            )
        assert "On-Chain Execution Failed" in str(exc_info.value)



def test_hub_bridge_live_system_status_and_tools():
    """Kiểm tra các tool của Smart Money Hub Bridge hoạt động ở chế độ LIVE."""
    from ecosystem.smart_money import hub_bridge

    status = hub_bridge.get_system_status()
    assert status.get("status") == "RUNNING"
    assert status.get("mode") == "STRICT_LIVE_ONCHAIN"

    # Kiểm tra analyze_token_risk trả về dữ liệu thật và live_verified=True
    risk = hub_bridge.analyze_token_risk("0x4200000000000000000000000000000000000006", chain="base")
    assert risk.get("live_verified") is True
    assert "safety_score" in risk
    assert "verdict" in risk

    # Kiểm tra run_bull_bear_debate
    debate = hub_bridge.run_bull_bear_debate("0x4200000000000000000000000000000000000006", chain="base")
    assert debate.get("live_verified") is True
    assert "consensus" in debate
    assert "bull_arguments" in debate
    assert "bear_arguments" in debate
