# NFTs: Collections, Minting & Marketplace

NFTs in Discoin work like real blockchain NFTs. They live on PoS networks (ARC/DSC), have rarity tiers, unique on-chain token hashes, ERC-721 contract addresses, and cost gas to mint. You can collect, trade, and display them. NFT values are included in your net worth.

## Browsing Collections

See all NFT collections on your server:

```
.nft collections
```

View details for a specific collection:

```
.nft view <symbol>
```

For example, `.nft view SCAMPS` shows the SCAMPS collection details, including its contract address, network, supply, mint price, and recent sales.

## Minting NFTs

Mint an NFT from any collection:

```
.nft mint <symbol>
```

**Cost:** The collection's mint price (in its denomination token) **plus gas** in the network's native coin. Just like a real on-chain mint transaction.

**What happens on mint:**
- A unique **token hash** is generated for your NFT (SHA256-based, like a real blockchain transaction)
- The NFT is assigned a sequential **token ID** within the collection
- Rarity is rolled (see table below)
- Your NFT is stored under the collection's **ERC-721 contract address**
- Gas is charged on the network

Minting is atomic  -  if any step fails (insufficient funds, gas, supply exhausted), the entire operation rolls back.

**Rarity rolls on mint:**

| Rarity    | Chance |
|-----------|--------|
| Common    | 50%    |
| Uncommon  | 25%    |
| Rare      | 15%    |
| Epic      | 8%     |
| Legendary | 2%     |

## Your Collection

View all your NFTs:

```
.nft inventory
```

Aliases: `.nft my`, `.nft inv`

View a specific NFT with its **full-size image** and blockchain details:

```
.nft view <symbol> <token_id>
```

For example, `.nft view SCAMPS 1` shows NFT #1 in the SCAMPS collection with its artwork, rarity, token hash, contract address, owner, and sale history.

## On-Chain Identity

Every NFT in Discoin has a proper blockchain identity:

- **Token Hash**  -  A unique SHA256-based hash identifying each individual NFT. Displayed as a shortened hex string (e.g. `a1b2c3d4...ef567890`).
- **Contract Address**  -  Each collection gets an ERC-721 contract address when deployed (e.g. `0xabcd1234...ef567890`).
- **Network**  -  NFTs live on either the Arcadia (ARC) or Discoin (DSC) network.
- **Token ID**  -  Sequential integer within the collection (e.g. #1, #2, #3...).

You can view all of these details with `.nft view <symbol> <token_id>`.

## Marketplace

### Listing for sale

List an NFT on the marketplace using the collection symbol and token ID:

```
.nft list <symbol> <token_id> <price>
```

For example: `.nft list TEST 1 10.5` lists NFT #1 from the TEST collection for 10.5 DSC.

Price is in the network's native coin (e.g. ARC for Arcadia NFTs, DSC for Discoin NFTs).

If the NFT is already listed, the command updates the price instead of creating a duplicate listing.

### Removing a listing

```
.nft unlist <symbol> <token_id>
```

### Browsing listings

```
.nft market
```

### Buying

```
.nft buy <symbol> <token_id>
```

For example: `.nft buy TEST 1` buys NFT #1 from the TEST collection at its listed price.

All marketplace transactions (listing, buying, transferring) are atomic with proper transaction guarantees.

### Sale History

View the sale history of any NFT:

```
.nft history <symbol> <token_id>
```

## Transfers

Send an NFT directly to another player:

```
.nft transfer @user <symbol> <token_id>
```

For example: `.nft transfer @Alice TEST 1` sends NFT #1 from the TEST collection to Alice.

Both players must be registered. The NFT moves to the recipient's inventory. Gas is charged on the NFT's network.

## Net Worth

NFT values are included in your net worth calculation. The value is based on:
1. Average sale price for that rarity tier (if sales exist)
2. Collection mint price (fallback)

## Deploying Collections (Protocol Dev+)

Players at **Protocol Dev** or **Exploiter** tier can deploy their own NFT collections:

```
.nft deploy <symbol> <name> <network> <mint_price> [max_supply]
```

**Arguments:**
- `symbol`  -  Short identifier for your collection (e.g. PUNKS, max 10 characters)
- `name`  -  Display name (use quotes for multi-word names: "Cool Punks")
- `network`  -  ARC or DSC (PoS networks only)
- `mint_price`  -  Cost to mint one NFT (in the network's native coin)
- `max_supply`  -  Optional maximum number of NFTs (omit for unlimited)

**What happens on deploy:**
- An **ERC-721 contract** is deployed on the network with a unique contract address
- Deployment gas is charged in the network's native coin
- Mint price is denominated in the network's native coin (ARC or DSC)
- Other players can mint from your collection
- The collection appears in `.nft collections`

**Example:**

```
.nft deploy PUNKS "Cool Punks" ARC 0.05 100
```

This deploys a 100-supply collection on Arcadia where each mint costs 0.05 ARC + gas.

## Deploying Tokens (Protocol Dev+)

Protocol Dev+ players can also deploy custom ERC-20 tokens:

```
.token deploy symbol=MYTKN name="My Token" emoji=🔥 network=ARC price=2.50
```

Optional parameters: `vol`, `burn_rate`, `fee`, `max_supply`, `supply`

See [Token Deployment](#token-deployment) for full details.

### Token Deployment

Deployed tokens have real on-chain contracts with configurable parameters:

| Parameter       | Description                                    | Example  |
|-----------------|------------------------------------------------|----------|
| `burn_rate`     | % of tokens burned on every transfer           | 0.01     |
| `fee`           | % transfer fee charged on transactions         | 0.005    |
| `max_supply`    | Maximum tokens that can ever exist              | 1000000  |
| `supply`        | Initial circulating supply (sent to deployer)   | 500000   |
| `vol`           | Daily price volatility (default 5%)             | 0.05     |

**What happens on deploy:**
1. Gas is charged in the network's native coin
2. An ERC-20 contract is created with your parameters
3. A liquidity pool (TOKEN/STABLECOIN) is auto-seeded
4. The token appears in `.crypto` and can be traded by anyone
5. Initial supply (if set) goes to your DeFi wallet

View any token's contract:

```
.token info <symbol>
```

## Gas Fees

All NFT operations charge gas on the collection's network, just like real blockchain transactions. Gas is paid in the network's native coin:

- **Arcadia Network:** Gas in ARC
- **Discoin Network:** Gas in DSC

Gas fees are calculated using the network's current base fee (EIP-1559 style) and scale with network activity.

## Website

The full NFT marketplace is available on the Discoin website at `/dashboard/nfts`. From the website you can:

- **Browse collections**  -  View all collections with contract addresses, supply progress, mint prices
- **Marketplace**  -  Browse all listed NFTs with sorting (price, rarity, recent) and filtering
- **My NFTs**  -  View your inventory with listing status
- **NFT details**  -  Click any NFT to see full details: blockchain identity, sale history, contract info
- **Collection details**  -  Click any collection to see all minted NFTs, floor price, total volume

## Command Reference

| Command | Description |
|---------|-------------|
| `.nft collections` | List all collections |
| `.nft view <symbol>` | View a collection |
| `.nft view <symbol> <token_id>` | View a specific NFT |
| `.nft mint <symbol>` | Mint an NFT |
| `.nft inventory` | View your NFTs |
| `.nft list <symbol> <token_id> <price>` | List for sale |
| `.nft unlist <symbol> <token_id>` | Remove listing |
| `.nft market` | Browse marketplace |
| `.nft buy <symbol> <token_id>` | Buy a listed NFT |
| `.nft history <symbol> <token_id>` | View sale history |
| `.nft transfer @user <symbol> <token_id>` | Transfer to player |
| `.nft deploy <symbol> <name> <network> <price> [supply]` | Deploy collection |
| `.token deploy symbol=X name=X network=X price=X` | Deploy custom token |
| `.token info <symbol>` | View token contract |
