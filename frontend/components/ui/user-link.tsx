"use client";

import Link from "next/link";
import { useUsernames } from "@/hooks/useUsernames";

interface UserLinkProps {
  userId: string;
  username?: string;
}

export function UserLink({ userId, username }: UserLinkProps) {
  const { names, loading } = useUsernames(username ? [] : [userId]);

  const displayName =
    username ?? names[userId] ?? (loading ? null : null);

  const label = displayName
    ? `@${displayName}`
    : `User ${userId.slice(0, 8)}`;

  return (
    <Link
      href={`/dashboard/profile/${userId}`}
      className="text-primary hover:underline"
    >
      {label}
    </Link>
  );
}
