"""Mining Pydantic models for the Discoin v2 API."""
from __future__ import annotations

from pydantic import BaseModel, Field


class MiningNetworkStats(BaseModel):
    """Stats for a Proof-of-Work mining network."""

    symbol: str = Field(..., description="Chain symbol (e.g. 'MTA', 'ARC').")
    block_height: int = Field(0, description="Current block height.")
    difficulty: float = Field(1.0, description="Current mining difficulty.")
    total_hashrate: float = Field(0.0, description="Total network hashrate.")
    current_reward: float = Field(0.0, description="Current block reward.")
    last_block_ts: str | None = Field(None, description="Timestamp of last mined block.")


class RigInfo(BaseModel):
    """Information about a mining rig type."""

    rig_id: str = Field(..., description="Rig type identifier.")
    name: str = Field("", description="Display name.")
    hashrate: float = Field(0.0, description="Hashrate per unit.")
    power: float = Field(0.0, description="Power consumption per unit.")
    price: float = Field(0.0, description="Purchase price per unit.")


class UserRigInfo(BaseModel):
    """A user's owned rig."""

    rig_id: str = Field(..., description="Rig type identifier.")
    quantity: int = Field(0, description="Number of rigs owned.")
    total_hashrate: float = Field(0.0, description="Total hashrate from this rig type.")


class MinerInfo(BaseModel):
    """Info about an active miner on the leaderboard."""

    user_id: int = Field(..., description="User ID.")
    username: str = Field("", description="Username.")
    total_hashrate: float = Field(0.0, description="Miner's total hashrate.")
    rig_count: int = Field(0, description="Total number of rigs.")
    blocks_mined: int = Field(0, description="Total blocks mined.")


class MiningGroupInfo(BaseModel):
    """Information about a mining group (pool)."""

    group_id: str = Field(..., description="Group identifier.")
    name: str = Field(..., description="Group display name.")
    description: str = Field("", description="Group description.")
    tag: str = Field("", description="Group tag.")
    founder_id: int = Field(0, description="Founder user ID.")
    member_count: int = Field(0, description="Number of members.")
    total_hashrate: float = Field(0.0, description="Combined hashrate of all members.")


class MiningGroupDetail(MiningGroupInfo):
    """Extended mining group info with member list."""

    members: list[MinerInfo] = Field(default_factory=list, description="Group members.")


class MiningBlockInfo(BaseModel):
    """Information about a mined block."""

    id: int = Field(..., description="Block record ID.")
    block_height: int = Field(0, description="Block height.")
    block_ts: str | None = Field(None, description="Block timestamp.")
    miner_id: int | None = Field(None, description="Miner user ID.")
    reward: float = Field(0.0, description="Block reward.")
    total_hashrate: float = Field(0.0, description="Network hashrate at time of mining.")


class UserMiningConfig(BaseModel):
    """User's current mining configuration and assignments."""

    total_hashrate: float = Field(0.0, description="User's total hashrate.")
    total_rigs: int = Field(0, description="Total rigs owned.")
    assignments: list[dict] = Field(default_factory=list, description="Rig-to-chain assignments.")
    group_id: str | None = Field(None, description="Mining group membership.")
