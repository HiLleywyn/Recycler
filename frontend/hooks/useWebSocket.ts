"use client";

import { useEffect, useCallback, useRef } from "react";
import wsClient from "@/lib/ws";
import { useAuthStore } from "@/stores/auth";

/**
 * Hook to connect to the WebSocket server.
 * Call once in a top-level layout to establish the connection.
 */
export function useWebSocketConnection() {
  const token = useAuthStore((s) => s.token);

  useEffect(() => {
    if (token) {
      wsClient.connect(token);
    }

    return () => {
      // Don't disconnect on unmount since other components may still need it.
      // The connection is cleaned up on logout.
    };
  }, [token]);

  return {
    isConnected: wsClient.isConnected,
    disconnect: () => wsClient.disconnect(),
  };
}

/**
 * Hook to subscribe to a WebSocket channel with automatic cleanup.
 *
 * @param channel - The channel name to subscribe to
 * @param handler - Callback invoked with each message's data payload
 * @param enabled - Whether the subscription is active (default: true)
 */
export function useWebSocket(
  channel: string | null,
  handler: (data: unknown) => void,
  enabled: boolean = true
) {
  const handlerRef = useRef(handler);

  useEffect(() => {
    handlerRef.current = handler;
  }, [handler]);

  const stableHandler = useCallback((data: unknown) => {
    handlerRef.current(data);
  }, []);

  useEffect(() => {
    if (!channel || !enabled) return;

    const unsubscribe = wsClient.subscribe(channel, stableHandler);
    return unsubscribe;
  }, [channel, enabled, stableHandler]);
}

/**
 * Hook to send messages over the WebSocket.
 */
export function useWebSocketSend() {
  return useCallback((channel: string, data: unknown) => {
    wsClient.send(channel, data);
  }, []);
}
