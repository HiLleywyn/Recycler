import axios, {
  AxiosInstance,
  AxiosRequestConfig,
  InternalAxiosRequestConfig,
} from "axios";
import type { ApiResponse, PaginatedResponse } from "@/types";

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "/api/v2";

let isRefreshing = false;
let failedQueue: Array<{
  resolve: (token: string) => void;
  reject: (error: unknown) => void;
}> = [];

function processQueue(error: unknown, token: string | null = null) {
  failedQueue.forEach((prom) => {
    if (error) {
      prom.reject(error);
    } else {
      prom.resolve(token!);
    }
  });
  failedQueue = [];
}

const api: AxiosInstance = axios.create({
  baseURL: API_BASE_URL,
  timeout: 15000,
  headers: {
    "Content-Type": "application/json",
  },
  withCredentials: true,  // Send httpOnly cookies with every request
});

// Request interceptor — attach auth token
api.interceptors.request.use(
  (config: InternalAxiosRequestConfig) => {
    if (typeof window !== "undefined") {
      const token = localStorage.getItem("discoin_token");
      if (token && config.headers) {
        config.headers.Authorization = `Bearer ${token}`;
      }
    }
    return config;
  },
  (error) => Promise.reject(error)
);

// Response interceptor — handle 401 with token refresh, retry on 429
api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const originalRequest = error.config as AxiosRequestConfig & {
      _retry?: boolean;
      _retryCount?: number;
    };

    // Rate limited — wait and retry (up to 3 times)
    if (error.response?.status === 429) {
      const retryCount = originalRequest._retryCount || 0;
      if (retryCount < 3) {
        originalRequest._retryCount = retryCount + 1;
        const retryAfter = error.response.headers?.["retry-after"];
        const waitMs = retryAfter ? parseInt(retryAfter, 10) * 1000 : 2000 * (retryCount + 1);
        await new Promise((r) => setTimeout(r, waitMs));
        return api(originalRequest);
      }
      return Promise.reject(error);
    }

    if (error.response?.status === 401 && !originalRequest._retry) {
      if (isRefreshing) {
        return new Promise((resolve, reject) => {
          failedQueue.push({ resolve, reject });
        })
          .then((token) => {
            if (originalRequest.headers) {
              originalRequest.headers.Authorization = `Bearer ${token}`;
            }
            return api(originalRequest);
          })
          .catch((err) => Promise.reject(err));
      }

      originalRequest._retry = true;
      isRefreshing = true;

      try {
        // Refresh token is sent automatically as httpOnly cookie
        const { data } = await axios.post(
          `${API_BASE_URL}/auth/refresh`,
          {},
          { withCredentials: true }
        );

        const newToken = data.access_token;
        localStorage.setItem("discoin_token", newToken);

        processQueue(null, newToken);

        if (originalRequest.headers) {
          originalRequest.headers.Authorization = `Bearer ${newToken}`;
        }
        return api(originalRequest);
      } catch (refreshError: unknown) {
        processQueue(refreshError, null);
        // Only logout if the refresh failed for auth reasons, not rate limiting
        const isRateLimited =
          refreshError &&
          typeof refreshError === "object" &&
          "response" in refreshError &&
          (refreshError as { response?: { status?: number } }).response?.status === 429;
        if (!isRateLimited) {
          localStorage.removeItem("discoin_token");
          localStorage.removeItem("discoin_user");
          if (typeof window !== "undefined") {
            window.location.href = "/";
          }
        }
        return Promise.reject(refreshError);
      } finally {
        isRefreshing = false;
      }
    }

    return Promise.reject(error);
  }
);

// ===== Typed wrapper functions =====

export async function get<T>(
  url: string,
  config?: AxiosRequestConfig
): Promise<ApiResponse<T>> {
  const response = await api.get<ApiResponse<T>>(url, config);
  return response.data;
}

export async function post<T>(
  url: string,
  data?: unknown,
  config?: AxiosRequestConfig
): Promise<ApiResponse<T>> {
  const response = await api.post<ApiResponse<T>>(url, data, config);
  return response.data;
}

export async function put<T>(
  url: string,
  data?: unknown,
  config?: AxiosRequestConfig
): Promise<ApiResponse<T>> {
  const response = await api.put<ApiResponse<T>>(url, data, config);
  return response.data;
}

export async function del<T>(
  url: string,
  config?: AxiosRequestConfig
): Promise<ApiResponse<T>> {
  const response = await api.delete<ApiResponse<T>>(url, config);
  return response.data;
}

export async function getPaginated<T>(
  url: string,
  config?: AxiosRequestConfig
): Promise<PaginatedResponse<T>> {
  const response = await api.get<PaginatedResponse<T>>(url, config);
  return response.data;
}

export default api;
