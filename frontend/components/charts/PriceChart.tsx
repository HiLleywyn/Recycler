"use client";

import { useEffect, useRef, useState } from "react";
import { createChart, CandlestickSeries, HistogramSeries } from "lightweight-charts";
import type { IChartApi, ISeriesApi, CandlestickData, HistogramData, Time } from "lightweight-charts";
import { useAuthStore } from "@/stores/auth";

interface PriceChartProps {
  symbol: string;
  timeframe?: string;
}

interface CandleRaw {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;
}

export function PriceChart({ symbol, timeframe = "1h" }: PriceChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const [loading, setLoading] = useState(true);
  const [empty, setEmpty] = useState(false);

  const token = useAuthStore((s) => s.token);

  // Create chart on mount
  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      layout: {
        background: { color: "rgb(20, 17, 28)" },
        textColor: "rgb(156, 163, 175)",
      },
      grid: {
        vertLines: { color: "rgba(255, 255, 255, 0.05)" },
        horzLines: { color: "rgba(255, 255, 255, 0.05)" },
      },
      crosshair: {
        vertLine: { color: "rgba(255, 255, 255, 0.1)", labelBackgroundColor: "rgb(30, 27, 40)" },
        horzLine: { color: "rgba(255, 255, 255, 0.1)", labelBackgroundColor: "rgb(30, 27, 40)" },
      },
      rightPriceScale: {
        borderColor: "rgba(255, 255, 255, 0.1)",
      },
      timeScale: {
        borderColor: "rgba(255, 255, 255, 0.1)",
        timeVisible: true,
        secondsVisible: false,
      },
    });

    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: "rgb(52, 211, 153)",
      downColor: "rgb(239, 68, 68)",
      borderUpColor: "rgb(52, 211, 153)",
      borderDownColor: "rgb(239, 68, 68)",
      wickUpColor: "rgb(52, 211, 153)",
      wickDownColor: "rgb(239, 68, 68)",
    });

    const volumeSeries = chart.addSeries(HistogramSeries, {
      priceFormat: { type: "volume" },
      priceScaleId: "volume",
    });

    chart.priceScale("volume").applyOptions({
      scaleMargins: { top: 0.8, bottom: 0 },
    });

    chartRef.current = chart;
    candleSeriesRef.current = candleSeries;
    volumeSeriesRef.current = volumeSeries;

    // ResizeObserver for responsive sizing
    const resizeObserver = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const { width, height } = entry.contentRect;
        chart.applyOptions({ width, height });
      }
    });
    resizeObserver.observe(containerRef.current);

    return () => {
      resizeObserver.disconnect();
      chart.remove();
      chartRef.current = null;
      candleSeriesRef.current = null;
      volumeSeriesRef.current = null;
    };
  }, []);

  // Fetch data when symbol or timeframe changes
  useEffect(() => {
    if (!candleSeriesRef.current || !volumeSeriesRef.current || !symbol) return;

    let cancelled = false;

    async function fetchCandles() {
      setLoading(true);
      setEmpty(false);

      try {
        const headers: Record<string, string> = {};
        if (token) headers.Authorization = `Bearer ${token}`;

        const res = await fetch(
          `/api/v2/market/candles/${encodeURIComponent(symbol)}?tf=${encodeURIComponent(timeframe)}&limit=200`,
          { headers }
        );

        if (!res.ok) throw new Error(`HTTP ${res.status}`);

        const data: CandleRaw[] = await res.json();

        if (cancelled) return;

        if (!data || data.length === 0) {
          setEmpty(true);
          setLoading(false);
          return;
        }

        const candles: CandlestickData<Time>[] = data.map((c) => ({
          time: c.time as Time,
          open: c.open,
          high: c.high,
          low: c.low,
          close: c.close,
        }));

        const volumes: HistogramData<Time>[] = data.map((c) => ({
          time: c.time as Time,
          value: c.volume ?? 0,
          color: c.close >= c.open ? "rgba(52, 211, 153, 0.3)" : "rgba(239, 68, 68, 0.3)",
        }));

        candleSeriesRef.current?.setData(candles);
        volumeSeriesRef.current?.setData(volumes);
        chartRef.current?.timeScale().fitContent();
      } catch {
        if (!cancelled) setEmpty(true);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    fetchCandles();

    return () => {
      cancelled = true;
    };
  }, [symbol, timeframe, token]);

  return (
    <div className="relative w-full" style={{ minHeight: 400 }}>
      {loading && (
        <div className="absolute inset-0 z-10 flex items-center justify-center bg-[rgb(20,17,28)]">
          <div className="flex flex-col items-center gap-2">
            <div className="size-6 animate-spin rounded-full border-2 border-primary border-t-transparent" />
            <span className="text-sm text-muted-foreground">Loading chart...</span>
          </div>
        </div>
      )}
      {empty && !loading && (
        <div className="absolute inset-0 z-10 flex items-center justify-center bg-[rgb(20,17,28)]">
          <p className="text-sm text-muted-foreground">No chart data available for {symbol}</p>
        </div>
      )}
      <div ref={containerRef} className="h-[400px] w-full" />
    </div>
  );
}
