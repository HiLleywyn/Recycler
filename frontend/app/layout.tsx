import type { Metadata, Viewport } from "next";
import { Inter, JetBrains_Mono, Space_Grotesk } from "next/font/google";
import { ThemeProvider } from "@/components/providers/theme-provider";
import { QueryProvider } from "@/components/providers/query-provider";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Toaster } from "@/components/ui/sonner";
import "./globals.css";

const inter = Inter({
  subsets: ["latin"],
  variable: "--font-inter",
  display: "swap",
});

const jetbrains = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--font-mono-jb",
  display: "swap",
});

const spaceGrotesk = Space_Grotesk({
  subsets: ["latin"],
  variable: "--font-space-grotesk",
  display: "swap",
  weight: ["500", "600", "700"],
});

export const metadata: Metadata = {
  title: {
    default: "Discoin - Discord Economy & Crypto Trading",
    template: "%s · Discoin",
  },
  description:
    "A full DeFi experience built for Discord communities. Trade, stake, mine, lend, play - inside the servers you already live in.",
  keywords: [
    "Discord bot",
    "crypto trading",
    "DeFi",
    "Discord economy",
    "token swap",
    "liquidity pools",
    "staking",
    "mining",
  ],
  openGraph: {
    title: "Discoin - Discord Economy & Crypto Trading",
    description:
      "Trade tokens, provide liquidity, stake rewards, mine blocks, and play games -all within your Discord server.",
    type: "website",
    siteName: "Discoin",
  },
  twitter: {
    card: "summary_large_image",
    title: "Discoin",
    description: "Discord Economy & Crypto Trading",
  },
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: dark)", color: "#0d1030" },
    { media: "(prefers-color-scheme: light)", color: "#ffffff" },
  ],
  colorScheme: "dark light",
};

// Runs before React hydrates to avoid a theme flash.
const themeBootScript = `(function(){try{var s=localStorage.getItem('discoin-theme')||'dark';var r=s==='system'?(window.matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light'):s;document.documentElement.classList.add(r);}catch(e){document.documentElement.classList.add('dark');}})();`;

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${inter.variable} ${jetbrains.variable} ${spaceGrotesk.variable}`}
      suppressHydrationWarning
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeBootScript }} />
      </head>
      <body className="min-h-screen bg-background font-sans antialiased">
        <ThemeProvider defaultTheme="dark" storageKey="discoin-theme">
          <QueryProvider>
            <TooltipProvider>
              {children}
              <Toaster />
            </TooltipProvider>
          </QueryProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
