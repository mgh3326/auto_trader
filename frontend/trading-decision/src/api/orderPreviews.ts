import {
  CreateOrderPreviewRequest,
  OrderPreviewSession,
} from "./types";

const BASE_URL = "/trading/api/order-previews";

export const orderPreviewsApi = {
  create: async (request: CreateOrderPreviewRequest): Promise<OrderPreviewSession> => {
    const res = await fetch(BASE_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request),
    });
    if (!res.ok) {
      throw new Error(`Failed to create order preview: ${res.statusText}`);
    }
    return res.json();
  },

  get: async (previewUuid: string): Promise<OrderPreviewSession> => {
    const res = await fetch(`${BASE_URL}/${previewUuid}`);
    if (!res.ok) {
      throw new Error(`Failed to fetch order preview: ${res.statusText}`);
    }
    return res.json();
  },

  refresh: async (previewUuid: string): Promise<OrderPreviewSession> => {
    const res = await fetch(`${BASE_URL}/${previewUuid}/refresh`, {
      method: "POST",
    });
    if (!res.ok) {
      throw new Error(`Failed to refresh order preview: ${res.statusText}`);
    }
    return res.json();
  },

  submit: async (
    previewUuid: string,
    approvalToken: string
  ): Promise<OrderPreviewSession> => {
    const res = await fetch(`${BASE_URL}/${previewUuid}/submit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approval_token: approvalToken }),
    });
    if (!res.ok) {
      if (res.status === 409) {
        const body = await res.json();
        throw new Error(`Submit blocked: ${body.detail}`);
      }
      throw new Error(`Failed to submit order: ${res.statusText}`);
    }
    return res.json();
  },
};
