import type { WSMessage } from "@/types";

type MessageHandler = (data: unknown) => void;

const WS_URL =
  process.env.NEXT_PUBLIC_WS_URL ||
  (typeof window !== "undefined"
    ? `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}/api/v2/ws`
    : "ws://localhost:8080/api/v2/ws");

const RECONNECT_BASE_DELAY = 1000;
const RECONNECT_MAX_DELAY = 30000;
const HEARTBEAT_INTERVAL = 30000;
const HEARTBEAT_TIMEOUT = 10000;

class WebSocketClient {
  private ws: WebSocket | null = null;
  private subscriptions = new Map<string, Set<MessageHandler>>();
  private reconnectAttempts = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  private heartbeatTimeoutTimer: ReturnType<typeof setTimeout> | null = null;
  private isIntentionallyClosed = false;
  private token: string | null = null;
  private _isConnected = false;

  get isConnected(): boolean {
    return this._isConnected;
  }

  connect(token?: string): void {
    if (this.ws?.readyState === WebSocket.OPEN) return;
    if (typeof window === "undefined") return;

    this.isIntentionallyClosed = false;
    this.token = token || this.token;

    try {
      // Never send auth token in the URL (it leaks to logs/history/referrers).
      // Instead, connect without auth and send token in the first message.
      this.ws = new WebSocket(WS_URL);

      this.ws.onopen = () => {
        this._isConnected = true;
        this.reconnectAttempts = 0;
        this.startHeartbeat();

        // Authenticate via message if we have a token
        if (this.token) {
          this.sendMessage({ type: "auth", channel: "", data: this.token, timestamp: Date.now() });
        }

        // Re-subscribe to all active channels
        for (const channel of this.subscriptions.keys()) {
          this.sendMessage({ type: "subscribe", channel, data: null, timestamp: Date.now() });
        }
      };

      this.ws.onmessage = (event: MessageEvent) => {
        try {
          const message: WSMessage = JSON.parse(event.data);

          if (message.type === "pong") {
            this.clearHeartbeatTimeout();
            return;
          }

          const handlers = this.subscriptions.get(message.channel);
          if (handlers) {
            handlers.forEach((handler) => handler(message.data));
          }

          // Also notify wildcard subscribers
          const wildcardHandlers = this.subscriptions.get("*");
          if (wildcardHandlers) {
            wildcardHandlers.forEach((handler) => handler(message));
          }
        } catch {
          // Ignore malformed messages
        }
      };

      this.ws.onclose = () => {
        this._isConnected = false;
        this.stopHeartbeat();

        if (!this.isIntentionallyClosed) {
          this.scheduleReconnect();
        }
      };

      this.ws.onerror = () => {
        // Error handling is done in onclose
      };
    } catch {
      this.scheduleReconnect();
    }
  }

  disconnect(): void {
    this.isIntentionallyClosed = true;
    this.stopHeartbeat();
    this.clearReconnectTimer();

    if (this.ws) {
      this.ws.close(1000, "Client disconnect");
      this.ws = null;
    }

    this._isConnected = false;
  }

  subscribe(channel: string, handler: MessageHandler): () => void {
    if (!this.subscriptions.has(channel)) {
      this.subscriptions.set(channel, new Set());

      // If already connected, send subscribe message
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.sendMessage({ type: "subscribe", channel, data: null, timestamp: Date.now() });
      }
    }

    this.subscriptions.get(channel)!.add(handler);

    // Return unsubscribe function
    return () => {
      this.unsubscribe(channel, handler);
    };
  }

  unsubscribe(channel: string, handler: MessageHandler): void {
    const handlers = this.subscriptions.get(channel);
    if (!handlers) return;

    handlers.delete(handler);

    if (handlers.size === 0) {
      this.subscriptions.delete(channel);

      if (this.ws?.readyState === WebSocket.OPEN) {
        this.sendMessage({ type: "unsubscribe", channel, data: null, timestamp: Date.now() });
      }
    }
  }

  send(channel: string, data: unknown): void {
    this.sendMessage({ type: "message", channel, data, timestamp: Date.now() });
  }

  private sendMessage(message: WSMessage): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(message));
    }
  }

  private scheduleReconnect(): void {
    this.clearReconnectTimer();

    const delay = Math.min(
      RECONNECT_BASE_DELAY * Math.pow(2, this.reconnectAttempts),
      RECONNECT_MAX_DELAY
    );

    this.reconnectTimer = setTimeout(() => {
      this.reconnectAttempts++;
      this.connect();
    }, delay);
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  private startHeartbeat(): void {
    this.stopHeartbeat();

    this.heartbeatTimer = setInterval(() => {
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.sendMessage({ type: "ping", channel: "", data: null, timestamp: Date.now() });
        this.heartbeatTimeoutTimer = setTimeout(() => {
          // No pong received — close and reconnect
          this.ws?.close();
        }, HEARTBEAT_TIMEOUT);
      }
    }, HEARTBEAT_INTERVAL);
  }

  private stopHeartbeat(): void {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
    this.clearHeartbeatTimeout();
  }

  private clearHeartbeatTimeout(): void {
    if (this.heartbeatTimeoutTimer) {
      clearTimeout(this.heartbeatTimeoutTimer);
      this.heartbeatTimeoutTimer = null;
    }
  }
}

// Singleton instance
const wsClient = new WebSocketClient();
export default wsClient;
