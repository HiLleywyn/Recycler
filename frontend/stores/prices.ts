import { create } from "zustand";
import type { PriceData } from "@/types";

interface PriceStore {
  prices: Map<string, PriceData>;
  lastUpdate: number | null;

  updatePrices: (updates: PriceData[]) => void;
  updatePrice: (symbol: string, data: Partial<PriceData>) => void;
  getPrice: (symbol: string) => PriceData | undefined;
  getAllPrices: () => PriceData[];
}

export const usePriceStore = create<PriceStore>((set, get) => ({
  prices: new Map(),
  lastUpdate: null,

  updatePrices: (updates) => {
    set((state) => {
      const newPrices = new Map(state.prices);
      for (const price of updates) {
        newPrices.set(price.symbol, { ...price, lastUpdate: Date.now() });
      }
      return { prices: newPrices, lastUpdate: Date.now() };
    });
  },

  updatePrice: (symbol, data) => {
    set((state) => {
      const newPrices = new Map(state.prices);
      const existing = newPrices.get(symbol);
      if (existing) {
        newPrices.set(symbol, { ...existing, ...data, lastUpdate: Date.now() });
      } else {
        newPrices.set(symbol, {
          symbol,
          price: 0,
          change24h: 0,
          volume24h: 0,
          high24h: 0,
          low24h: 0,
          lastUpdate: Date.now(),
          ...data,
        } as PriceData);
      }
      return { prices: newPrices, lastUpdate: Date.now() };
    });
  },

  getPrice: (symbol) => {
    return get().prices.get(symbol);
  },

  getAllPrices: () => {
    return Array.from(get().prices.values());
  },
}));
