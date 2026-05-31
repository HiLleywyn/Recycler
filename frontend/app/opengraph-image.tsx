import { ImageResponse } from "next/og";

export const dynamic = "force-static";
export const alt = "Discoin - Discord Economy & Crypto Trading";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

export default async function Image() {
  return new ImageResponse(
    (
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          justifyContent: "space-between",
          width: "100%",
          height: "100%",
          padding: "72px 80px",
          background:
            "radial-gradient(circle at 15% 15%, rgba(124,92,255,0.45), transparent 55%), radial-gradient(circle at 85% 85%, rgba(90,235,224,0.30), transparent 55%), #0B0D1F",
          fontFamily: "system-ui, -apple-system, BlinkMacSystemFont, sans-serif",
          color: "white",
        }}
      >
        {/* Top row: mark + wordmark */}
        <div style={{ display: "flex", alignItems: "center", gap: 20 }}>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              width: 84,
              height: 84,
              borderRadius: 24,
              background:
                "linear-gradient(135deg, #7C5CFF 0%, #4FB3FF 55%, #5AEBE0 100%)",
            }}
          >
            <div
              style={{
                width: 68,
                height: 68,
                borderRadius: 18,
                background: "#0B0D1F",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                fontSize: 42,
                fontWeight: 800,
                color: "#5AEBE0",
              }}
            >
              D
            </div>
          </div>
          <div
            style={{
              fontSize: 40,
              fontWeight: 700,
              letterSpacing: "-0.02em",
              background:
                "linear-gradient(90deg, #7C5CFF 0%, #4FB3FF 60%, #5AEBE0 100%)",
              backgroundClip: "text",
              color: "transparent",
            }}
          >
            Discoin
          </div>
        </div>

        {/* Headline */}
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 18,
            maxWidth: 900,
          }}
        >
          <div
            style={{
              fontSize: 84,
              fontWeight: 700,
              lineHeight: 1.02,
              letterSpacing: "-0.03em",
              background:
                "linear-gradient(90deg, #ffffff 0%, #c7d6ff 100%)",
              backgroundClip: "text",
              color: "transparent",
            }}
          >
            Discord Economy, Reimagined.
          </div>
          <div
            style={{
              fontSize: 30,
              color: "rgba(220, 228, 255, 0.75)",
              lineHeight: 1.35,
            }}
          >
            Trade tokens, stake rewards, mine blocks, lend, predict,
            and play - all inside the servers you already live in.
          </div>
        </div>

        {/* Feature chips */}
        <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
          {["Swaps", "Liquidity", "Staking", "Mining", "Games", "NFTs"].map(
            (f) => (
              <div
                key={f}
                style={{
                  padding: "12px 22px",
                  borderRadius: 999,
                  border: "1px solid rgba(255,255,255,0.14)",
                  background: "rgba(255,255,255,0.04)",
                  fontSize: 22,
                  color: "rgba(220, 228, 255, 0.9)",
                }}
              >
                {f}
              </div>
            ),
          )}
        </div>
      </div>
    ),
    { ...size },
  );
}
