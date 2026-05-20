import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import { User } from '@/lib/types/auth';
import { ChatMessage } from '@/lib/types/candidate';
import { EmailTemplate } from '@/lib/types/campaign';

export type PlanType = 'email' | 'linkedin' | 'both';

interface AppState {
  // Auth
  user: User | null;
  setUser: (user: User | null) => void;

  // Onboarding flow
  candidateId: number | null;
  setCandidateId: (id: number | null) => void;

  chatHistory: ChatMessage[];
  addChatMessage: (msg: ChatMessage) => void;
  clearChatHistory: () => void;

  // Lead discovery
  selectedTier: 200 | 350 | 500;
  setSelectedTier: (tier: 200 | 350 | 500) => void;

  // Plan selection (email / linkedin / both)
  planType: PlanType;
  setPlanType: (t: PlanType) => void;

  // Campaign
  selectedTemplate: EmailTemplate | null;
  setSelectedTemplate: (t: EmailTemplate | null) => void;

  selectedStyles: string[];
  setSelectedStyles: (styles: string[]) => void;

  campaignId: number | null;
  setCampaignId: (id: number | null) => void;

  emailAccountId: number | null;
  setEmailAccountId: (id: number | null) => void;

  linkedInCampaignId: number | null;
  setLinkedInCampaignId: (id: number | null) => void;

  // Order tracking
  orderId: number | null;
  setOrderId: (id: number | null) => void;

  // Current step in onboarding
  currentStep: number;
  setCurrentStep: (step: number) => void;
}

export const useAppStore = create<AppState>()(persist((set) => ({
  user: null,
  setUser: (user) => set({ user }),

  candidateId: null,
  setCandidateId: (candidateId) => set({ candidateId }),

  chatHistory: [],
  addChatMessage: (msg) => set((s) => ({ chatHistory: [...s.chatHistory, msg] })),
  clearChatHistory: () => set({ chatHistory: [] }),

  selectedTier: 350,
  setSelectedTier: (selectedTier) => set({ selectedTier }),

  planType: 'email',
  setPlanType: (planType) => set({ planType }),

  selectedTemplate: null,
  setSelectedTemplate: (selectedTemplate) => set({ selectedTemplate }),

  selectedStyles: [],
  setSelectedStyles: (selectedStyles) => set({ selectedStyles }),

  campaignId: null,
  setCampaignId: (campaignId) => set({ campaignId }),

  emailAccountId: null,
  setEmailAccountId: (emailAccountId) => set({ emailAccountId }),

  linkedInCampaignId: null,
  setLinkedInCampaignId: (linkedInCampaignId) => set({ linkedInCampaignId }),

  orderId: null,
  setOrderId: (orderId) => set({ orderId }),

  currentStep: 1,
  setCurrentStep: (currentStep) => set({ currentStep }),
}), {
  name: 'internreach-app-store',
  partialize: (state) => ({
    user: state.user,
    candidateId: state.candidateId,
    currentStep: state.currentStep,
    selectedTier: state.selectedTier,
    planType: state.planType,
    selectedStyles: state.selectedStyles,
    campaignId: state.campaignId,
    emailAccountId: state.emailAccountId,
    linkedInCampaignId: state.linkedInCampaignId,
    orderId: state.orderId,
  }),
}));
