"use client";

import { useState, useMemo } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Terminal, Search } from "lucide-react";

interface CommandEntry {
  name: string;
  description: string;
}

interface CommandCategory {
  name: string;
  commands: CommandEntry[];
}

const commandCategories: CommandCategory[] = [
  {
    name: "Economy",
    commands: [
      { name: "/balance", description: "View your wallet and bank balance" },
      { name: "/bank deposit", description: "Deposit USD into your bank" },
      { name: "/bank withdraw", description: "Withdraw USD from your bank" },
      { name: "/bank transfer", description: "Transfer USD to another user" },
      { name: "/bank move", description: "Move tokens between your wallet and bank" },
      { name: "/send", description: "Send tokens to another user" },
      { name: "/wallet", description: "View your DeFi wallet holdings" },
      { name: "/earn work", description: "Work a job to earn tokens" },
      { name: "/earn daily", description: "Claim your daily reward" },
      { name: "/earn jobs", description: "List available jobs" },
      { name: "/earn promote", description: "Promote to a better job" },
      { name: "/drop", description: "Manually spawn a token airdrop (admin)" },
      { name: "/airdrop", description: "Airdrop tokens to everyone in the channel" },
    ],
  },
  {
    name: "Trading",
    commands: [
      { name: "/trade buy", description: "Buy a token on the CeFi market (supports $ prefix: .buy ARC $50 or .buy $50 ARC)" },
      { name: "/trade sell", description: "Sell a token on the CeFi market (supports $ prefix: .sell ARC $100)" },
      { name: "/trade swap", description: "Swap tokens via an AMM pool" },
      { name: "/trade prices", description: "View live token prices" },
      { name: "/trade portfolio", description: "View your crypto portfolio" },
      { name: "/trade info", description: "View detailed info for a token" },
      { name: "/trade chart", description: "Display a price chart for a token" },
      { name: "/trade list", description: "List all available tokens" },
      { name: "/crypto buy", description: "Buy crypto tokens (use $ for dollar amounts: .buy ARC $50)" },
      { name: "/crypto sell", description: "Sell crypto tokens (use $ for dollar amounts: .sell ARC $100)" },
      { name: "/crypto portfolio", description: "View your crypto portfolio overview" },
      { name: "/crypto info", description: "Token market info and stats" },
    ],
  },
  {
    name: "Liquidity Pools",
    commands: [
      { name: "/trade pool list", description: "List all AMM liquidity pools" },
      { name: "/trade pool add", description: "Add liquidity to a pool" },
      { name: "/trade pool remove", description: "Remove liquidity from a pool" },
      { name: "/trade pool price", description: "Get the current AMM pool price" },
      { name: "/trade pool swap", description: "Swap tokens through an AMM pool" },
      { name: "/trade pool chart", description: "View pool price chart" },
    ],
  },
  {
    name: "Lending & Savings",
    commands: [
      { name: "/bank borrow", description: "Borrow USD using crypto as collateral" },
      { name: "/bank repay", description: "Repay an outstanding loan" },
      { name: "/bank status", description: "View your active loan status" },
      { name: "/bank rates", description: "View current lending interest rates" },
      { name: "/bank savings deposit", description: "Deposit into a savings pool to earn interest" },
      { name: "/bank savings withdraw", description: "Withdraw from a savings pool" },
      { name: "/bank savings rates", description: "View current savings APY rates" },
      { name: "/bank sun", description: "Wrap tokens into SUN (interest-bearing)" },
      { name: "/bank unsun", description: "Unwrap SUN back to the underlying token" },
    ],
  },
  {
    name: "Staking",
    commands: [
      { name: "/stake list", description: "List all NPC staking pools and yields" },
      { name: "/stake farm", description: "Stake tokens in an NPC farm to earn yield" },
      { name: "/stake unstake", description: "Unstake tokens from a farm" },
      { name: "/stake mine", description: "Stake tokens in a mining node" },
      { name: "/stake networks", description: "View available staking networks" },
      { name: "/stake stats", description: "View your staking statistics" },
    ],
  },
  {
    name: "Validators",
    commands: [
      { name: "/validator register", description: "Register your node as a PoS validator" },
      { name: "/validator unregister", description: "Unregister your validator node" },
      { name: "/validator commission", description: "Update your validator commission rate" },
      { name: "/validator delegate", description: "Delegate tokens to a validator" },
      { name: "/validator undelegate", description: "Remove your delegation from a validator" },
      { name: "/validator delegations", description: "View all your delegations" },
      { name: "/validator list", description: "List all active validators" },
      { name: "/validator mempool", description: "View the PoS transaction mempool" },
      { name: "/validator submit", description: "Submit a PoS transaction to the mempool" },
      { name: "/validator networks", description: "List networks that support PoS validation" },
      { name: "/validator stats", description: "View validator network statistics" },
    ],
  },
  {
    name: "Mining",
    commands: [
      { name: "/chain mine solo", description: "Mine a block solo on a network" },
      { name: "/chain mine pool", description: "Join the pool and mine collaboratively" },
      { name: "/chain mine group", description: "Mine with your group" },
      { name: "/chain rigs buy", description: "Purchase mining rigs" },
      { name: "/chain rigs sell", description: "Sell mining rigs" },
      { name: "/chain rigs assign", description: "Assign rigs to a network" },
      { name: "/chain rigs status", description: "View your mining rig status" },
      { name: "/chain rigs history", description: "View your mining history" },
      { name: "/chain block", description: "View the latest or a specific block" },
      { name: "/chain tx", description: "Look up a transaction by hash" },
    ],
  },
  {
    name: "Mining Groups",
    commands: [
      { name: "/group create", description: "Create a new mining group" },
      { name: "/group join", description: "Join a public mining group" },
      { name: "/group invite", description: "Invite a user to your group" },
      { name: "/group accept", description: "Accept a group invitation" },
      { name: "/group decline", description: "Decline a group invitation" },
      { name: "/group leave", description: "Leave your current mining group" },
      { name: "/group info", description: "View info about a group" },
      { name: "/group list", description: "List all public mining groups" },
      { name: "/group rename", description: "Rename your group" },
      { name: "/group kick", description: "Kick a member from your group" },
      { name: "/group disband", description: "Disband your group" },
    ],
  },
  {
    name: "Smart Contracts",
    commands: [
      { name: "/contract deploy", description: "Deploy a smart contract" },
      { name: "/contract call", description: "Call a function on a deployed contract" },
      { name: "/contract info", description: "View details of a deployed contract" },
      { name: "/contract list", description: "List all deployed contracts" },
      { name: "/contract events", description: "View events emitted by a contract" },
      { name: "/contract fund", description: "Fund a contract with tokens" },
      { name: "/contract withdraw", description: "Withdraw from a contract" },
      { name: "/contract pause", description: "Pause a contract" },
      { name: "/contract resume", description: "Resume a paused contract" },
    ],
  },
  {
    name: "Games",
    commands: [
      { name: "/play coinflip", description: "Flip a coin — double your bet or lose it all" },
      { name: "/play dice", description: "Roll the dice and predict the outcome" },
      { name: "/play slots", description: "Spin the slot reels for a chance at jackpots" },
      { name: "/play roulette", description: "Bet on a color, number, or range and spin the wheel" },
      { name: "/play blackjack", description: "Play a hand of blackjack against the dealer" },
      { name: "/play mines", description: "Reveal tiles and avoid hidden mines for big multipliers" },
      { name: "/play stats", description: "View your gambling statistics" },
    ],
  },
  {
    name: "Shop & Inventory",
    commands: [
      { name: "/shop list", description: "Browse available shop items" },
      { name: "/shop buy", description: "Purchase an item from the shop" },
      { name: "/inventory", description: "View your item inventory" },
      { name: "/inventory use", description: "Use an item from your inventory" },
      { name: "/inventory sell", description: "Sell an item back" },
      { name: "/inventory transfer", description: "Transfer an item to another user" },
      { name: "/inventory levelup", description: "Level up an item" },
    ],
  },
  {
    name: "Social & Info",
    commands: [
      { name: "/leaderboard", description: "View the server wealth leaderboard" },
      { name: "/balance", description: "View your own or another user's balance" },
      { name: "/notify", description: "Manage your DM notification preferences" },
      { name: "/report", description: "Report a bug or issue to the server admins" },
      { name: "/help", description: "Display the help menu and command overview" },
      { name: "/help ask", description: "Ask the AI assistant a question" },
    ],
  },
  {
    name: "Security",
    commands: [
      { name: "/2fa setup", description: "Set up two-factor authentication (TOTP)" },
      { name: "/2fa disable", description: "Disable two-factor authentication" },
    ],
  },
  {
    name: "Admin",
    commands: [
      { name: "/admin give", description: "Give tokens to a user" },
      { name: "/admin take", description: "Take tokens from a user" },
      { name: "/admin setbal", description: "Set a user's balance" },
      { name: "/admin resetuser", description: "Reset a user's economy data" },
      { name: "/admin resetserver", description: "Reset the entire server economy" },
      { name: "/admin module", description: "Enable or disable a feature module" },
      { name: "/admin setchannel", description: "Set a bot output channel" },
      { name: "/admin addtoken", description: "Add a new token to the server" },
      { name: "/admin removetoken", description: "Remove a token from the server" },
      { name: "/admin addnetwork", description: "Add a blockchain network" },
      { name: "/admin addvalidator", description: "Add an NPC validator" },
      { name: "/admin setprice", description: "Manually set a token price" },
      { name: "/admin status", description: "View bot and economy health status" },
      { name: "/admin log", description: "View the admin audit log" },
      { name: "/admin chain", description: "Chain multiple commands with && (admin-only beta)" },
    ],
  },
];

export default function CommandsPage() {
  const [search, setSearch] = useState("");

  const filteredCategories = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return commandCategories;
    return commandCategories
      .map((cat) => ({
        ...cat,
        commands: cat.commands.filter(
          (cmd) =>
            cmd.name.toLowerCase().includes(q) ||
            cmd.description.toLowerCase().includes(q)
        ),
      }))
      .filter((cat) => cat.commands.length > 0);
  }, [search]);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Commands</h1>
          <p className="text-sm text-muted-foreground">
            All available Discord bot commands
          </p>
        </div>
        <div className="relative w-64">
          <Search className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            placeholder="Search commands..."
            className="pl-9"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
      </div>

      {filteredCategories.length === 0 ? (
        <p className="text-sm text-muted-foreground">No commands match your search.</p>
      ) : (
        <div className="space-y-4">
          {filteredCategories.map((category) => (
            <Card key={category.name}>
              <CardHeader>
                <CardTitle className="flex items-center gap-2">
                  <Terminal className="size-4" />
                  {category.name}
                </CardTitle>
              </CardHeader>
              <CardContent>
                <div className="space-y-2">
                  {category.commands.map((cmd) => (
                    <div
                      key={cmd.name}
                      className="flex items-center justify-between rounded-lg px-3 py-2 transition-colors hover:bg-muted/50"
                    >
                      <div className="flex items-center gap-3">
                        <Badge
                          variant="secondary"
                          className="font-mono text-xs"
                        >
                          {cmd.name}
                        </Badge>
                        <span className="text-sm text-muted-foreground">
                          {cmd.description}
                        </span>
                      </div>
                    </div>
                  ))}
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
