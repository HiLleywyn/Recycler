"""
core/framework/command_parser.py  -  Fuzzy NLP command parser for Discoin.

Turns messy, typo-laden user input into structured ChainStep objects.
Handles:
  - Fuzzy command names:  .by → buy, .buyy → buy, .shel → sell, .sale → sell,
                          .mov → move, .moves → move
  - Flexible argument order:  43 MTA, MTA 43, $699 of MTA, MTA $30
  - Filler word stripping:  "of", "muh", "my", "some", "worth", "to", "from"
  - Storage shortcuts:  b → bank, w → wallet, c → cash, v → vault
  - Dollar amounts:  $699, $30, $1.5k

Usage:
    from core.framework.command_parser import CommandParser

    parser = CommandParser(known_tokens={"MTA", "ARC", "DSC", "SUN", ...})
    step = parser.parse("buyy $699 of MTA")
    # => ChainStep(action=BUY, symbol="MTA", amount=AmountSpec(resolved=699, is_usd=True))
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from core.framework.amount_parser import parse_amount, AmountSpec
from core.framework.chain_engine import ActionType, ChainStep


# ═══════════════════════════════════════════════════════════════════════════
# Command alias table  -  maps fuzzy/short names to canonical ActionType
# ═══════════════════════════════════════════════════════════════════════════

# Exact aliases (checked first, before fuzzy matching)
_COMMAND_ALIASES: dict[str, ActionType] = {
    # ── buy ──
    "buy":       ActionType.BUY,
    "purchase":  ActionType.BUY,
    "acquire":   ActionType.BUY,
    "grab":      ActionType.BUY,
    "get":       ActionType.BUY,
    "long":      ActionType.BUY,
    # ── sell ──
    "sell":      ActionType.SELL,
    "sale":      ActionType.SELL,
    "dump":      ActionType.SELL,
    "unload":    ActionType.SELL,
    "short":     ActionType.SELL,
    "liquidate": ActionType.SELL,
    # ── move ──
    "move":      ActionType.MOVE,
    "mv":        ActionType.MOVE,
    "transfer":  ActionType.TRANSFER,
    "send":      ActionType.TRANSFER,
    "pay":       ActionType.TRANSFER,
    # ── swap ──
    "swap":      ActionType.SWAP,
    "exchange":  ActionType.SWAP,
    "convert":   ActionType.SWAP,
    "trade":     ActionType.SWAP,
    # ── deposit / withdraw ──
    "deposit":   ActionType.DEPOSIT,
    "dep":       ActionType.DEPOSIT,
    "withdraw":  ActionType.WITHDRAW,
    "with":      ActionType.WITHDRAW,
    # ── stake / unstake / delegate ──
    "stake":      ActionType.STAKE,
    "unstake":    ActionType.UNSTAKE,
    "lock":       ActionType.STAKE,
    "delegate":   ActionType.DELEGATE,
    "del":        ActionType.DELEGATE,
    "undelegate": ActionType.UNDELEGATE,
    "undel":      ActionType.UNDELEGATE,
    # ── liquidity pools ──
    "addlp":          ActionType.ADD_LP,
    "add_lp":         ActionType.ADD_LP,
    "lp":             ActionType.ADD_LP,
    "addliquidity":   ActionType.ADD_LP,
    "liquidityadd":   ActionType.ADD_LP,
    "rmlp":           ActionType.REMOVE_LP,
    "removelp":       ActionType.REMOVE_LP,
    "remove_lp":      ActionType.REMOVE_LP,
    "removeliquidity": ActionType.REMOVE_LP,
    "liquidityremove": ActionType.REMOVE_LP,
    "withdrawlp":     ActionType.REMOVE_LP,
    # ── savings ──
    "save":           ActionType.SAVE_DEPOSIT,
    "saving":         ActionType.SAVE_DEPOSIT,
    "savedeposit":    ActionType.SAVE_DEPOSIT,
    "savingsdeposit": ActionType.SAVE_DEPOSIT,
    "unsave":         ActionType.SAVE_WITHDRAW,
    "savingswithdraw": ActionType.SAVE_WITHDRAW,
    "savewithdraw":   ActionType.SAVE_WITHDRAW,
    # ── buy rig ──
    "buyrig":     ActionType.BUY_RIG,
    "buy_rig":    ActionType.BUY_RIG,
    "rig":        ActionType.BUY_RIG,
    "miner":      ActionType.BUY_RIG,
    "getrig":     ActionType.BUY_RIG,
    # ── shop buy ──
    "shopbuy":    ActionType.SHOP_BUY,
    "shop_buy":   ActionType.SHOP_BUY,
    "item":       ActionType.SHOP_BUY,
    "buyitem":    ActionType.SHOP_BUY,
    # ── play / gamble (PASSTHROUGH so chain executor replays through real command system) ──
    "play":       ActionType.PASSTHROUGH,
    "game":       ActionType.PASSTHROUGH,
    "bet":        ActionType.PASSTHROUGH,
    "gamble":     ActionType.PASSTHROUGH,
    "coinflip":   ActionType.PASSTHROUGH,
    "cf":         ActionType.PASSTHROUGH,
    "slots":      ActionType.PASSTHROUGH,
    "sl":         ActionType.PASSTHROUGH,
    "dice":       ActionType.PASSTHROUGH,
    "blackjack":  ActionType.PASSTHROUGH,
    "bj":         ActionType.PASSTHROUGH,
    "roulette":   ActionType.PASSTHROUGH,
    "mines":      ActionType.PASSTHROUGH,
    # ── query (read-only checks) ──
    "check":      ActionType.QUERY,
    "query":      ActionType.QUERY,
    "price":      ActionType.QUERY,
    "info":       ActionType.QUERY,
    "status":     ActionType.QUERY,
    "lookup":     ActionType.QUERY,
    # ── create wallet ──
    "createwallet":  ActionType.CREATE_WALLET,
    "create_wallet": ActionType.CREATE_WALLET,
    "newwallet":     ActionType.CREATE_WALLET,
    "mkwallet":      ActionType.CREATE_WALLET,
    # ── notifications ──
    "notify":        ActionType.SET_NOTIFICATION,
    "setnotify":     ActionType.SET_NOTIFICATION,
    "notification":  ActionType.SET_NOTIFICATION,
    "notifications": ActionType.SET_NOTIFICATION,
    "alert":         ActionType.SET_NOTIFICATION,
    # ── passthrough (bot commands replayed as-is) ──
    "admin":      ActionType.PASSTHROUGH,
    "work":       ActionType.PASSTHROUGH,
    "mine":       ActionType.PASSTHROUGH,
    "wallet":     ActionType.PASSTHROUGH,
    "help":       ActionType.PASSTHROUGH,
    "chain":      ActionType.PASSTHROUGH,
    "group":      ActionType.PASSTHROUGH,
    "mg":         ActionType.PASSTHROUGH,
    "bank":       ActionType.PASSTHROUGH,
    "shop":       ActionType.PASSTHROUGH,
    "crypto":     ActionType.PASSTHROUGH,
    "discoin":    ActionType.PASSTHROUGH,
    "daily":      ActionType.PASSTHROUGH,
    "crash":      ActionType.PASSTHROUGH,
    "leaderboard": ActionType.PASSTHROUGH,
    "lb":         ActionType.PASSTHROUGH,
    "profile":    ActionType.PASSTHROUGH,
    "bal":        ActionType.PASSTHROUGH,
    "balance":    ActionType.PASSTHROUGH,
    "portfolio":  ActionType.PASSTHROUGH,
    "port":       ActionType.PASSTHROUGH,
    "loan":       ActionType.PASSTHROUGH,
    "savings":    ActionType.PASSTHROUGH,
    "pools":      ActionType.PASSTHROUGH,
    "validators": ActionType.PASSTHROUGH,
    "contracts":  ActionType.PASSTHROUGH,
    "settings":   ActionType.PASSTHROUGH,
    "report":     ActionType.PASSTHROUGH,
    "drops":      ActionType.PASSTHROUGH,
    "chart":      ActionType.PASSTHROUGH,
    "explorer":   ActionType.PASSTHROUGH,
    "rugpull":    ActionType.PASSTHROUGH,
    "rug":        ActionType.PASSTHROUGH,
    "ape":        ActionType.PASSTHROUGH,
    "airdrop":    ActionType.PASSTHROUGH,
    "gambling":   ActionType.PASSTHROUGH,
    "inventory":  ActionType.PASSTHROUGH,
    "inv":        ActionType.PASSTHROUGH,
    "predict":    ActionType.PASSTHROUGH,
    "rugstats":   ActionType.PASSTHROUGH,
    "rughistory": ActionType.PASSTHROUGH,
    "tokeninfo":  ActionType.PASSTHROUGH,
    "ti":         ActionType.PASSTHROUGH,
}

# All canonical command names for fuzzy matching
_CANONICAL_NAMES: list[str] = list(_COMMAND_ALIASES.keys())

# ═══════════════════════════════════════════════════════════════════════════
# Filler words to strip from arguments
# ═══════════════════════════════════════════════════════════════════════════

_FILLER_WORDS: frozenset[str] = frozenset({
    "of", "muh", "my", "some", "worth", "the", "a", "an",
    "please", "pls", "plz", "bruh", "bro", "fam", "lol",
    "into", "outta", "from", "for",
})

# Words that indicate "from" / "to" for move commands
_FROM_INDICATORS: frozenset[str] = frozenset({"from", "outta", "out"})
_TO_INDICATORS: frozenset[str] = frozenset({"to", "into", "in", "2"})

# ═══════════════════════════════════════════════════════════════════════════
# Storage location aliases (reuses bank.py convention)
# ═══════════════════════════════════════════════════════════════════════════

STORAGE_ALIASES: dict[str, str] = {
    "cash": "cash", "c": "cash", "pocket": "cash",
    "bank": "bank", "b": "bank", "cefi": "bank",
    "wallet": "wallet", "w": "wallet", "defi": "wallet",
    "vault": "vault", "v": "vault", "save": "vault", "savings": "vault",
}


# ═══════════════════════════════════════════════════════════════════════════
# Parse result
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ParseResult:
    """Result of parsing a single command string."""
    step: ChainStep | None = None
    error: str | None = None
    suggestion: str | None = None   # "Did you mean ...?"
    confidence: float = 0.0
    raw: str = ""


# ═══════════════════════════════════════════════════════════════════════════
# CommandParser
# ═══════════════════════════════════════════════════════════════════════════

class CommandParser:
    """Fuzzy NLP command parser.

    Accepts messy user input and produces structured :class:`ChainStep` objects.
    Requires a set of known token symbols for disambiguation.
    """

    def __init__(self, known_tokens: set[str] | None = None) -> None:
        self.known_tokens: set[str] = {t.upper() for t in (known_tokens or set())}
        # Build a lowercase lookup for fuzzy token matching
        self._token_lower: dict[str, str] = {t.lower(): t for t in self.known_tokens}

    # ── Public API ──────────────────────────────────────────────────────

    def parse(self, raw: str) -> ParseResult:
        """Parse a single command string into a :class:`ParseResult`.

        Examples::

            parse("buy 43 MTA")
            parse("buyy $699 of MTA")
            parse("shel MTA $30")
            parse("sale 0.1 MTA")
            parse("mov all muh MTA b w")
            parse("moves $30 of MTA b 2 w")
        """
        raw = raw.strip()
        if not raw:
            return ParseResult(error="Empty command", raw=raw)

        # Split into tokens
        tokens = raw.split()
        cmd_token = tokens[0].lower().rstrip(".,!?")
        arg_tokens = tokens[1:]

        # ── Resolve command ──────────────────────────────────────────
        action, confidence, suggestion = self._resolve_command(cmd_token)

        if action is None:
            return ParseResult(
                error=f"Unknown command: {cmd_token}",
                suggestion=suggestion,
                raw=raw,
            )

        # ── Passthrough: preserve full text for bot command replay ──
        if action == ActionType.PASSTHROUGH:
            step = ChainStep(
                action=ActionType.PASSTHROUGH,
                confidence=1.0,
                source_text=raw,
            )
            return ParseResult(step=step, confidence=1.0, raw=raw)

        # ── Strip filler words from args ─────────────────────────────
        cleaned = self._strip_filler(arg_tokens)

        # ── Route to action-specific parser ──────────────────────────
        if action in (ActionType.BUY, ActionType.SELL):
            return self._parse_trade(action, cleaned, confidence, raw)

        if action == ActionType.PLAY_GAME:
            # cmd_token is the game name when user wrote e.g. "coinflip 100"
            return self._parse_play_game(cmd_token, cleaned, confidence, raw)

        if action == ActionType.MOVE:
            return self._parse_move(cleaned, confidence, raw)

        if action == ActionType.SWAP:
            return self._parse_swap(cleaned, confidence, raw)

        if action in (ActionType.DEPOSIT, ActionType.WITHDRAW):
            return self._parse_deposit_withdraw(action, cleaned, confidence, raw)

        if action in (ActionType.STAKE, ActionType.UNSTAKE,
                      ActionType.DELEGATE, ActionType.UNDELEGATE):
            return self._parse_stake(action, cleaned, confidence, raw)

        if action == ActionType.TRANSFER:
            return self._parse_transfer(cleaned, confidence, raw)

        if action in (ActionType.ADD_LP, ActionType.REMOVE_LP):
            return self._parse_lp(action, cleaned, confidence, raw)

        if action in (ActionType.SAVE_DEPOSIT, ActionType.SAVE_WITHDRAW):
            return self._parse_save(action, cleaned, confidence, raw)

        if action == ActionType.BUY_RIG:
            return self._parse_rig(cleaned, confidence, raw)

        if action == ActionType.SHOP_BUY:
            return self._parse_shop_buy(cleaned, confidence, raw)

        if action == ActionType.QUERY:
            return self._parse_query(cleaned, confidence, raw)

        if action == ActionType.CREATE_WALLET:
            return self._parse_create_wallet(cleaned, confidence, raw)

        if action == ActionType.SET_NOTIFICATION:
            return self._parse_set_notification(cleaned, confidence, raw)

        # Fallback: build a generic step
        step = ChainStep(
            action=action,
            confidence=confidence,
            source_text=raw,
        )
        return ParseResult(step=step, confidence=confidence, raw=raw)

    # ── Command resolution ──────────────────────────────────────────────

    def _resolve_command(self, cmd: str) -> tuple[ActionType | None, float, str | None]:
        """Resolve a possibly-misspelled command to an ActionType.

        Returns (action, confidence, suggestion_if_no_match).
        """
        # 1. Exact match
        if cmd in _COMMAND_ALIASES:
            return _COMMAND_ALIASES[cmd], 1.0, None

        # 2. Fuzzy match against all known command aliases
        matches = difflib.get_close_matches(cmd, _CANONICAL_NAMES, n=1, cutoff=0.5)
        if matches:
            matched = matches[0]
            # Calculate confidence: ratio of similarity
            ratio = difflib.SequenceMatcher(None, cmd, matched).ratio()
            return _COMMAND_ALIASES[matched], ratio, matched

        return None, 0.0, None

    # ── Filler stripping ────────────────────────────────────────────────

    def _strip_filler(self, tokens: list[str]) -> list[str]:
        """Remove filler words while preserving meaningful tokens."""
        result: list[str] = []
        for t in tokens:
            low = t.lower().rstrip(".,!?")
            # Keep the token if it's a known token symbol, a number, a $-amount,
            # a storage alias, or "all"/"half"/etc.
            if (
                low in _FILLER_WORDS
                and low not in self._token_lower
                and low not in STORAGE_ALIASES
                and not self._is_amount_like(t)
            ):
                continue
            result.append(t)
        return result

    # ── Trade parser (buy/sell) ─────────────────────────────────────────

    def _parse_trade(
        self, action: ActionType, args: list[str], confidence: float, raw: str
    ) -> ParseResult:
        """Parse buy/sell: flexible <amount> <symbol> or <symbol> <amount>."""
        if not args:
            return ParseResult(
                error=f"Usage: {action.value} <amount> <symbol>",
                confidence=confidence,
                raw=raw,
            )

        symbol, amount_spec = self._extract_symbol_amount(args)

        if not symbol:
            return ParseResult(
                error=f"Could not identify a token symbol in: {' '.join(args)}",
                confidence=confidence,
                raw=raw,
            )

        if amount_spec is None:
            return ParseResult(
                error=f"Could not parse amount in: {' '.join(args)}",
                confidence=confidence,
                raw=raw,
            )

        step = ChainStep(
            action=action,
            symbol=symbol,
            amount=amount_spec,
            confidence=confidence,
            source_text=raw,
        )
        return ParseResult(step=step, confidence=confidence, raw=raw)

    # ── Move parser ─────────────────────────────────────────────────────

    def _parse_move(
        self, args: list[str], confidence: float, raw: str
    ) -> ParseResult:
        """Parse move: <amount> <token> <from> [to] <to>

        Handles:
          - move all MTA b w
          - move $30 of MTA b 2 w
          - move all muh MTA bank to wallet
          - move 100 ARC from bank to wallet
        """
        if len(args) < 3:
            return ParseResult(
                error="Usage: move <amount> <token> <from> <to>",
                confidence=confidence,
                raw=raw,
            )

        # Special case: "move everything <from> <to>"  -  batch all assets, no token needed
        first = args[0].lower().rstrip(".,!?") if args else ""
        if first == "everything" and len(args) >= 3:
            from_loc_ev, to_loc_ev = self._extract_locations(args[1:])
            if from_loc_ev and to_loc_ev:
                step = ChainStep(
                    action=ActionType.PASSTHROUGH,
                    symbol=None,
                    amount=None,
                    confidence=confidence,
                    source_text=raw,
                    params={"cmd": f"bank move everything {from_loc_ev} {to_loc_ev}"},
                )
                return ParseResult(step=step, confidence=confidence, raw=raw)

        # Extract symbol and amount from the leading tokens
        symbol = None
        amount_spec = None
        remaining: list[str] = []

        # First pass: find symbol and amount, collect remaining for locations
        amount_tokens: list[str] = []
        found_symbol = False

        for t in args:
            low = t.lower().rstrip(".,!?")
            if not found_symbol and low in self._token_lower:
                symbol = self._token_lower[low]
                found_symbol = True
            elif not found_symbol and low.upper() in self.known_tokens:
                symbol = low.upper()
                found_symbol = True
            elif low in STORAGE_ALIASES or low in _TO_INDICATORS or low in _FROM_INDICATORS:
                remaining.append(t)
            elif self._is_amount_like(t):
                amount_tokens.append(t)
            else:
                # Could be a mangled token name  -  try fuzzy
                fuzzy_sym = self._fuzzy_token(low)
                if fuzzy_sym and not found_symbol:
                    symbol = fuzzy_sym
                    found_symbol = True
                else:
                    remaining.append(t)

        # Parse amount
        if amount_tokens:
            amount_spec = parse_amount(" ".join(amount_tokens))
        else:
            # Default to "all" if no amount specified but symbol found
            amount_spec = parse_amount("all")

        if not symbol:
            return ParseResult(
                error="Could not identify a token symbol for move.",
                confidence=confidence,
                raw=raw,
            )

        # Parse locations from remaining tokens
        from_loc, to_loc = self._extract_locations(remaining)

        if not from_loc or not to_loc:
            return ParseResult(
                error="Could not identify from/to locations. Use: b(ank), w(allet), c(ash), v(ault).",
                confidence=confidence,
                raw=raw,
            )

        # Derive direction hint for ServiceBridge
        _to_bank_locs = {"bank", "vault"}
        _to_wallet_locs = {"wallet", "cash"}
        if to_loc in _to_wallet_locs:
            direction = "to_wallet"
        elif to_loc in _to_bank_locs:
            direction = "to_bank"
        else:
            direction = "to_wallet"

        step = ChainStep(
            action=ActionType.MOVE,
            symbol=symbol,
            amount=amount_spec,
            confidence=confidence,
            source_text=raw,
            params={
                "from": from_loc,
                "to": to_loc,
                "source": from_loc,
                "destination": to_loc,
                "direction": direction,
            },
            target=to_loc,
        )
        return ParseResult(step=step, confidence=confidence, raw=raw)

    def _extract_locations(self, tokens: list[str]) -> tuple[str | None, str | None]:
        """Extract from/to storage locations from a list of tokens.

        Handles: "b w", "b 2 w", "bank to wallet", "from bank to wallet"
        """
        locations: list[str] = []
        skip_next = False

        for i, t in enumerate(tokens):
            if skip_next:
                skip_next = False
                continue
            low = t.lower().rstrip(".,!?")

            # Skip directional indicators
            if low in _TO_INDICATORS or low in _FROM_INDICATORS:
                continue

            loc = STORAGE_ALIASES.get(low)
            if loc:
                locations.append(loc)

        if len(locations) >= 2:
            return locations[0], locations[1]
        if len(locations) == 1:
            return locations[0], None
        return None, None

    # ── Swap parser ─────────────────────────────────────────────────────

    def _parse_swap(
        self, args: list[str], confidence: float, raw: str
    ) -> ParseResult:
        """Parse swap: <amount> <symbol_in> <symbol_out> or <symbol_in> <symbol_out> <amount>"""
        if len(args) < 2:
            return ParseResult(
                error="Usage: swap <amount> <token_in> <token_out>",
                confidence=confidence,
                raw=raw,
            )

        symbols: list[str] = []
        amount_tokens: list[str] = []

        for t in args:
            low = t.lower().rstrip(".,!?")
            if low in self._token_lower:
                symbols.append(self._token_lower[low])
            elif low.upper() in self.known_tokens:
                symbols.append(low.upper())
            elif self._is_amount_like(t):
                amount_tokens.append(t)
            else:
                fuzzy = self._fuzzy_token(low)
                if fuzzy:
                    symbols.append(fuzzy)

        if len(symbols) < 2:
            return ParseResult(
                error="Need two token symbols for swap.",
                confidence=confidence,
                raw=raw,
            )

        amount_spec = parse_amount(" ".join(amount_tokens)) if amount_tokens else parse_amount("all")

        step = ChainStep(
            action=ActionType.SWAP,
            symbol=symbols[0],
            amount=amount_spec,
            confidence=confidence,
            source_text=raw,
            params={"symbol_out": symbols[1], "token_in": symbols[0], "token_out": symbols[1]},
        )
        return ParseResult(step=step, confidence=confidence, raw=raw)

    # ── Deposit/Withdraw parser ─────────────────────────────────────────

    def _parse_deposit_withdraw(
        self, action: ActionType, args: list[str], confidence: float, raw: str
    ) -> ParseResult:
        if not args:
            return ParseResult(
                error=f"Usage: {action.value} <amount>",
                confidence=confidence,
                raw=raw,
            )

        amount_spec = parse_amount(" ".join(args))
        step = ChainStep(
            action=action,
            amount=amount_spec,
            confidence=confidence,
            source_text=raw,
        )
        return ParseResult(step=step, confidence=confidence, raw=raw)

    # ── Stake/Unstake parser ────────────────────────────────────────────

    def _parse_stake(
        self, action: ActionType, args: list[str], confidence: float, raw: str
    ) -> ParseResult:
        if not args:
            return ParseResult(
                error=f"Usage: {action.value} <amount> <symbol> [validator]",
                confidence=confidence,
                raw=raw,
            )

        symbol, amount_spec = self._extract_symbol_amount(args)
        # Remaining tokens after symbol/amount could be validator name
        leftover = [
            t for t in args
            if t.lower() not in self._token_lower
            and t.upper() not in self.known_tokens
            and not self._is_amount_like(t)
        ]
        validator = " ".join(leftover) if leftover else None

        step = ChainStep(
            action=action,
            symbol=symbol,
            amount=amount_spec or parse_amount("all"),
            confidence=confidence,
            source_text=raw,
            target=validator,
            params={"validator": validator} if validator else {},
        )
        return ParseResult(step=step, confidence=confidence, raw=raw)

    # ── Transfer parser ─────────────────────────────────────────────────

    def _parse_transfer(
        self, args: list[str], confidence: float, raw: str
    ) -> ParseResult:
        if not args:
            return ParseResult(
                error="Usage: transfer <user> <amount>",
                confidence=confidence,
                raw=raw,
            )

        # Look for a mention or user-like token
        target = None
        amount_tokens: list[str] = []

        for t in args:
            if t.startswith("<@") and t.endswith(">"):
                target = t
            elif self._is_amount_like(t):
                amount_tokens.append(t)
            elif target is None:
                target = t

        amount_spec = parse_amount(" ".join(amount_tokens)) if amount_tokens else None

        step = ChainStep(
            action=ActionType.TRANSFER,
            amount=amount_spec,
            confidence=confidence,
            source_text=raw,
            target=target,
        )
        return ParseResult(step=step, confidence=confidence, raw=raw)

    # ── LP parser ───────────────────────────────────────────────────────

    def _parse_lp(
        self, action: ActionType, args: list[str], confidence: float, raw: str
    ) -> ParseResult:
        """Parse add/remove liquidity: <amount> <token_a> <token_b>

        Examples::
            lp 100 ARC USDC
            addlp 50 DSC DSD
            removelp 25% ARC USDC
            rmlp all DSC DSD
        """
        if len(args) < 2:
            return ParseResult(
                error=f"Usage: {'addlp' if action == ActionType.ADD_LP else 'removelp'} <amount> <token_a> <token_b>",
                confidence=confidence,
                raw=raw,
            )

        symbols: list[str] = []
        amount_tokens: list[str] = []

        for t in args:
            low = t.lower().rstrip(".,!?")
            if low in self._token_lower:
                symbols.append(self._token_lower[low])
            elif low.upper() in self.known_tokens:
                symbols.append(low.upper())
            elif self._is_amount_like(t):
                amount_tokens.append(t)
            else:
                fuzzy = self._fuzzy_token(low)
                if fuzzy:
                    symbols.append(fuzzy)

        if len(symbols) < 2:
            return ParseResult(
                error="Need two token symbols for liquidity (e.g. ARC USDC).",
                confidence=confidence,
                raw=raw,
            )

        amount_spec = parse_amount(" ".join(amount_tokens)) if amount_tokens else parse_amount("all")

        step = ChainStep(
            action=action,
            symbol=symbols[0],
            amount=amount_spec,
            confidence=confidence,
            source_text=raw,
            target=symbols[1],
            params={"token_a": symbols[0], "token_b": symbols[1]},
        )
        return ParseResult(step=step, confidence=confidence, raw=raw)

    # ── Savings deposit/withdraw parser ─────────────────────────────────

    def _parse_save(
        self, action: ActionType, args: list[str], confidence: float, raw: str
    ) -> ParseResult:
        """Parse savings deposit/withdraw: [symbol] <amount>

        Examples::
            save 100
            save 100 USD
            save 50 ARC
            unsave all
            savewithdraw half ARC
        """
        symbol = None
        amount_tokens: list[str] = []

        for t in args:
            low = t.lower().rstrip(".,!?")
            if symbol is None and (low in self._token_lower or low.upper() in self.known_tokens):
                symbol = self._token_lower.get(low) or low.upper()
            elif self._is_amount_like(t):
                amount_tokens.append(t)
            elif symbol is None:
                fuzzy = self._fuzzy_token(low)
                if fuzzy:
                    symbol = fuzzy

        amount_spec = parse_amount(" ".join(amount_tokens)) if amount_tokens else parse_amount("all")

        step = ChainStep(
            action=action,
            symbol=symbol or "USD",
            amount=amount_spec,
            confidence=confidence,
            source_text=raw,
        )
        return ParseResult(step=step, confidence=confidence, raw=raw)

    # ── Buy rig parser ──────────────────────────────────────────────────

    def _parse_rig(
        self, args: list[str], confidence: float, raw: str
    ) -> ParseResult:
        """Parse buy rig: [rig_type] [quantity]

        Examples::
            rig basic
            buyrig advanced 2
            miner gpu 3
        """
        rig_type: str | None = None
        quantity_tokens: list[str] = []

        for t in args:
            low = t.lower().rstrip(".,!?")
            if self._is_amount_like(t):
                quantity_tokens.append(t)
            elif rig_type is None:
                rig_type = low

        amount_spec = parse_amount(" ".join(quantity_tokens)) if quantity_tokens else None

        step = ChainStep(
            action=ActionType.BUY_RIG,
            amount=amount_spec,
            confidence=confidence,
            source_text=raw,
            target=rig_type,
            params={"rig_type": rig_type or "basic"},
        )
        return ParseResult(step=step, confidence=confidence, raw=raw)

    # ── Shop buy parser ─────────────────────────────────────────────────

    def _parse_shop_buy(
        self, args: list[str], confidence: float, raw: str
    ) -> ParseResult:
        """Parse shop buy: <item> [quantity]

        Examples::
            shopbuy hashstone
            item lockstone 2
            buyitem liqstone
        """
        item_name: str | None = None
        quantity_tokens: list[str] = []

        for t in args:
            low = t.lower().rstrip(".,!?")
            if self._is_amount_like(t) and item_name is not None:
                quantity_tokens.append(t)
            elif item_name is None:
                item_name = low

        amount_spec = parse_amount(" ".join(quantity_tokens)) if quantity_tokens else None

        if not item_name:
            return ParseResult(
                error="Usage: shopbuy <item_name> [quantity]",
                confidence=confidence,
                raw=raw,
            )

        step = ChainStep(
            action=ActionType.SHOP_BUY,
            amount=amount_spec,
            confidence=confidence,
            source_text=raw,
            target=item_name,
            params={"item": item_name},
        )
        return ParseResult(step=step, confidence=confidence, raw=raw)

    # ── Play game parser ────────────────────────────────────────────────

    _GAME_NAMES: frozenset[str] = frozenset({
        "coinflip", "cf", "flip", "slots", "sl", "dice",
        "blackjack", "bj", "roulette", "rou", "mines", "play",
    })

    def _parse_play_game(
        self, cmd_token: str, args: list[str], confidence: float, raw: str
    ) -> ParseResult:
        """Parse play game: [game_name] <amount> [token] [side/options]

        Examples::
            coinflip 100
            play slots 50
            play blackjack 200 ARC
            dice 100 2
            cf 50 heads
        """
        # Game name may be the command itself (e.g. "coinflip") or first arg (e.g. "play coinflip")
        game_name: str | None = None
        amount_tokens: list[str] = []
        symbol: str | None = None
        extra_params: dict = {}

        # If cmd_token is a known game name, use it directly
        if cmd_token in self._GAME_NAMES and cmd_token != "play":
            game_name = cmd_token
            tokens_to_scan = args
        else:
            tokens_to_scan = args

        for t in tokens_to_scan:
            low = t.lower().rstrip(".,!?")
            # First non-amount word might be game name if not set
            if game_name is None and low in self._GAME_NAMES:
                game_name = low
            elif symbol is None and (low in self._token_lower or low.upper() in self.known_tokens):
                symbol = self._token_lower.get(low) or low.upper()
            elif self._is_amount_like(t):
                amount_tokens.append(t)
            elif game_name and low in ("heads", "tails", "red", "black", "even", "odd"):
                extra_params["side"] = low

        amount_spec = parse_amount(" ".join(amount_tokens)) if amount_tokens else None

        if not game_name:
            game_name = "play"

        step = ChainStep(
            action=ActionType.PLAY_GAME,
            symbol=symbol or "USD",
            amount=amount_spec,
            confidence=confidence,
            source_text=raw,
            target=game_name,
            params={"game": game_name, "token": symbol or "USD", **extra_params},
        )
        return ParseResult(step=step, confidence=confidence, raw=raw)

    # ── Query parser ────────────────────────────────────────────────────

    def _parse_query(
        self, args: list[str], confidence: float, raw: str
    ) -> ParseResult:
        """Parse read-only queries: check <symbol|topic>

        Examples::
            check MTA
            price ARC
            info DSC
            status
        """
        symbol: str | None = None
        query_type = "market.price_single"
        topic: str | None = None

        for t in args:
            low = t.lower().rstrip(".,!?")
            if symbol is None and (low in self._token_lower or low.upper() in self.known_tokens):
                symbol = self._token_lower.get(low) or low.upper()
            elif low in ("portfolio", "port", "holdings", "balance", "bal"):
                query_type = "portfolio.holdings"
            elif low in ("stakes", "staking", "staked"):
                query_type = "portfolio.stakes"
            elif low in ("savings", "save"):
                query_type = "portfolio.savings"
            elif low in ("loans", "loan"):
                query_type = "portfolio.loans"
            elif low in ("prices", "market"):
                query_type = "market.prices"
            elif topic is None:
                topic = low

        step = ChainStep(
            action=ActionType.QUERY,
            symbol=symbol,
            confidence=confidence,
            source_text=raw,
            params={
                "query_type": query_type,
                "intent_id": query_type,
                "topic": topic or symbol or "",
            },
        )
        return ParseResult(step=step, confidence=confidence, raw=raw)

    # ── Create wallet parser ────────────────────────────────────────────

    def _parse_create_wallet(
        self, args: list[str], confidence: float, raw: str
    ) -> ParseResult:
        """Parse create wallet: <network>

        Examples::
            createwallet arc
            newwallet discoin
            createwallet mta
        """
        network: str | None = None

        for t in args:
            low = t.lower().rstrip(".,!?")
            if network is None:
                network = low

        if not network:
            return ParseResult(
                error="Usage: createwallet <network>  (e.g. arc, mta, discoin, sun)",
                confidence=confidence,
                raw=raw,
            )

        step = ChainStep(
            action=ActionType.CREATE_WALLET,
            confidence=confidence,
            source_text=raw,
            target=network,
            params={"network": network},
        )
        return ParseResult(step=step, confidence=confidence, raw=raw)

    # ── Set notification parser ─────────────────────────────────────────

    def _parse_set_notification(
        self, args: list[str], confidence: float, raw: str
    ) -> ParseResult:
        """Parse set notification: [type] [on|off|value]

        Examples::
            notify price MTA
            notify on
            alert off
            setnotify price_alert ARC 10%
        """
        notif_type: str | None = None
        notif_value: str | None = None
        symbol: str | None = None

        for t in args:
            low = t.lower().rstrip(".,!?")
            if low in ("on", "off", "enable", "disable", "all"):
                notif_value = low
            elif low in ("price", "price_alert", "whale", "drop", "pump", "stake", "unstake"):
                notif_type = low
            elif symbol is None and (low in self._token_lower or low.upper() in self.known_tokens):
                symbol = self._token_lower.get(low) or low.upper()
            elif notif_type is None:
                notif_type = low

        step = ChainStep(
            action=ActionType.SET_NOTIFICATION,
            symbol=symbol,
            confidence=confidence,
            source_text=raw,
            target=notif_type,
            params={
                "notification_type": notif_type or "price",
                "value": notif_value or "on",
                "symbol": symbol or "",
            },
        )
        return ParseResult(step=step, confidence=confidence, raw=raw)

    # ── Helpers ─────────────────────────────────────────────────────────

    def _extract_symbol_amount(
        self, tokens: list[str]
    ) -> tuple[str | None, AmountSpec | None]:
        """Extract a token symbol and amount from a mixed list of tokens.

        Handles all orderings: "43 MTA", "MTA 43", "$699 MTA", "MTA $30"
        """
        symbol = None
        amount_tokens: list[str] = []

        for t in tokens:
            low = t.lower().rstrip(".,!?")
            # Check exact token match
            if low in self._token_lower and symbol is None:
                symbol = self._token_lower[low]
            elif low.upper() in self.known_tokens and symbol is None:
                symbol = low.upper()
            elif self._is_amount_like(t):
                amount_tokens.append(t)
            elif symbol is None:
                # Try fuzzy token match
                fuzzy = self._fuzzy_token(low)
                if fuzzy:
                    symbol = fuzzy

        amount_spec = parse_amount(" ".join(amount_tokens)) if amount_tokens else None

        return symbol, amount_spec

    def _fuzzy_token(self, name: str) -> str | None:
        """Fuzzy-match a token name against known tokens."""
        if not name or len(name) < 2:
            return None
        matches = difflib.get_close_matches(
            name, list(self._token_lower.keys()), n=1, cutoff=0.7
        )
        if matches:
            return self._token_lower[matches[0]]
        return None

    @staticmethod
    def _is_amount_like(token: str) -> bool:
        """Check if a token looks like an amount expression."""
        t = token.lower().rstrip(".,!?")
        # Dollar amounts
        if t.startswith("$"):
            return True
        # "all", "half", fractions, etc.
        if t in ("all", "everything", "half", "quarter", "third", "max",
                 "rest", "remaining", "full", "total", "entire",
                 "eighth", "fifth", "tenth", "two", "three", "four"):
            return True
        # Numeric (possibly with k/m/b suffix or commas)
        if re.match(r"^[\d,.]+[kmb]?$", t):
            return True
        # Fraction notation
        if re.match(r"^\d+/\d+$", t):
            return True
        return False
