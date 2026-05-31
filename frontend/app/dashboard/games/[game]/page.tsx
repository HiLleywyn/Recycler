import GamePageClient from "./client";

export function generateStaticParams() {
  return [
    { game: "coinflip" },
    { game: "dice" },
    { game: "blackjack" },
    { game: "crash" },
    { game: "mines" },
    { game: "slots" },
    { game: "roulette" },
    { game: "plinko" },
    { game: "wheel" },
  ];
}

export default function GamePage() {
  return <GamePageClient />;
}
