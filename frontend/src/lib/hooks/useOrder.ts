import { useCallback } from 'react';
import { useAppStore } from '@/store/useAppStore';
import api from '@/lib/api';

/**
 * Hook for order lifecycle operations.
 * Creates, updates, and tracks outreach orders throughout the flow.
 */
export function useOrder() {
  const { orderId, setOrderId } = useAppStore();

  /** Create a new order (call at resume upload). Returns the new order ID. */
  const createOrder = useCallback(async (candidateId?: number) => {
    try {
      const res = await api.post('/orders/create', {
        candidate_id: candidateId || null,
      });
      const newOrderId = res.data.order_id;
      setOrderId(newOrderId);
      return newOrderId;
    } catch (e) {
      console.error('[Order] Failed to create order:', e);
      return null;
    }
  }, [setOrderId]);

  /** Update order status and/or linked IDs. */
  const updateOrder = useCallback(async (updates: {
    status?: string;
    candidate_id?: number;
    campaign_id?: number;
    email_account_id?: number;
    leads_collected?: number;
    log_entry?: string;
  }) => {
    if (!orderId) return;
    try {
      await api.post(`/orders/${orderId}/update`, updates);
    } catch (e) {
      console.error('[Order] Failed to update order:', e);
    }
  }, [orderId]);

  /** Fetch active order on app load (if any). */
  const loadActiveOrder = useCallback(async () => {
    try {
      const res = await api.get('/orders/active');
      const order = res.data?.order;
      if (order) {
        setOrderId(order.id);
        return order;
      }
      return null;
    } catch {
      return null;
    }
  }, [setOrderId]);

  return { orderId, createOrder, updateOrder, loadActiveOrder };
}
