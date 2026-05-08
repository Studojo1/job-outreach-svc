export interface LinkedInCampaign {
  id: number;
  name: string;
  status: 'draft' | 'running' | 'paused' | 'completed';
  target_role: string;
  target_industries: string[];
  target_locations: string[];
  target_company_sizes: string[];
  target_keywords: string | null;
  connection_note: string | null;
  followup_message: string | null;
  daily_limit: number;
  total_leads: number;
  total_sent: number;
  total_accepted: number;
  total_followups_sent: number;
  total_replied: number;
  launched_at: string | null;
  created_at: string;
}

export interface CampaignStats {
  campaign_id: number;
  status: string;
  total_leads: number;
  total_sent: number;
  total_accepted: number;
  total_followups_sent: number;
  total_replied: number;
  acceptance_rate: number;
  reply_rate: number;
}

export interface ConnectionRequest {
  id: number;
  name: string;
  headline: string | null;
  company: string | null;
  profile_url: string;
  connection_note: string | null;
  followup_message: string | null;
  status: string;
  sent_at: string | null;
  accepted_at: string | null;
  followup_sent_at: string | null;
  reply_text: string | null;
  reply_sentiment: string | null;
}
