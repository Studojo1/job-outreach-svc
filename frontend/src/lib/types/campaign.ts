export interface EmailTemplate {
  id: number;
  name: string;
  subject: string;
  body: string;
}

export interface CampaignMetrics {
  campaign_id: number;
  campaign_name: string;
  status: string;
  emails_total: number;
  emails_queued: number;
  emails_sent: number;
  emails_failed: number;
  emails_replied: number;
  reply_rate: number;
}
