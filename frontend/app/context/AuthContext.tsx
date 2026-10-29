"use client";

import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { fetchWithAuth } from "../../lib/api";

interface User {
  id: string;
  email: string;
  tenant_id: string;
  roles: string[];
  display_name?: string | null;
  avatar?: string | null;
  account_status?: string;
  totp_enabled?: boolean;
  onboarding_completed?: boolean;
}

interface ProfileResponse {
  user_id: string;
  tenant_id: string;
  email: string;
  roles: string[];
  display_name: string | null;
  avatar: string | null;
  status: string;
  totp_enabled: boolean;
  onboarding_completed: boolean;
}

interface AuthContextType {
  user: User | null;
  loading: boolean;
  profileReady: boolean;
  userDisabled: boolean;
  login: (token: string, tenantId: string, user: User, remember?: boolean) => void;
  updateUserProfile: (profile: Partial<User>) => void;
  refreshProfile: () => Promise<ProfileResponse>;
  logout: () => void;
}

export const LOCAL_OPERATOR_USER: User = {
  id: "00000000-0000-0000-0000-000000000002",
  email: "admin@averqel.local",
  tenant_id: "00000000-0000-0000-0000-000000000001",
  roles: [
    "admin",
    "staff",
    "feedback_manager",
    "collection_moderator",
    "support_manager",
  ],
  display_name: "Local Operator",
  avatar: null,
  account_status: "active",
  totp_enabled: false,
  onboarding_completed: true,
};

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<User | null>(LOCAL_OPERATOR_USER);
  const [loading, setLoading] = useState(false);
  const profileReady = true;
  const userDisabled = false;

  const updateUserProfile = useCallback((profile: Partial<User>) => {
    setUser((current) => (current ? { ...current, ...profile } : current));
  }, []);

  const refreshProfile = useCallback(async (): Promise<ProfileResponse> => {
    try {
      const response = (await fetchWithAuth("/auth/profile")) as Response;
      if (response && response.ok) {
        const profile = (await response.json()) as ProfileResponse;
        if (profile && typeof profile.user_id === "string") {
          setUser((current) => ({
            ...(current ?? LOCAL_OPERATOR_USER),
            email: profile.email,
            tenant_id: profile.tenant_id,
            roles: profile.roles.filter((role): role is string => typeof role === "string"),
            display_name: profile.display_name,
            avatar: profile.avatar,
            account_status: profile.status,
            totp_enabled: profile.totp_enabled,
            onboarding_completed: profile.onboarding_completed,
          }));
          return profile;
        }
      }
    } catch {
      // Offline local-first fallback
    }

    return {
      user_id: LOCAL_OPERATOR_USER.id,
      tenant_id: LOCAL_OPERATOR_USER.tenant_id,
      email: LOCAL_OPERATOR_USER.email,
      roles: LOCAL_OPERATOR_USER.roles,
      display_name: LOCAL_OPERATOR_USER.display_name ?? null,
      avatar: null,
      status: "active",
      totp_enabled: false,
      onboarding_completed: true,
    };
  }, []);

  useEffect(() => {
    if (typeof window !== "undefined") {
      if (!localStorage.getItem("averqel_token")) {
        localStorage.setItem("averqel_token", "local-token");
      }
      if (!localStorage.getItem("averqel_tenant_id")) {
        localStorage.setItem("averqel_tenant_id", LOCAL_OPERATOR_USER.tenant_id);
      }
      if (!localStorage.getItem("averqel_user")) {
        localStorage.setItem("averqel_user", JSON.stringify(LOCAL_OPERATOR_USER));
      }
    }
  }, []);

  const login = (token: string, tenantId: string, userData: User) => {
    if (typeof window !== "undefined") {
      localStorage.setItem("averqel_token", token);
      localStorage.setItem("averqel_tenant_id", tenantId);
      localStorage.setItem("averqel_user", JSON.stringify(userData));
    }
    setUser(userData);
  };

  const logout = () => {
    // In local-first mode without a login system, reset to the default local user
    if (typeof window !== "undefined") {
      localStorage.setItem("averqel_token", "local-token");
      localStorage.setItem("averqel_tenant_id", LOCAL_OPERATOR_USER.tenant_id);
      localStorage.setItem("averqel_user", JSON.stringify(LOCAL_OPERATOR_USER));
    }
    setUser(LOCAL_OPERATOR_USER);
  };

  return (
    <AuthContext.Provider
      value={{
        user,
        loading,
        profileReady,
        userDisabled,
        login,
        updateUserProfile,
        refreshProfile,
        logout,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (context === undefined) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return context;
}
