"use client";

import { useState, useCallback } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Users,
  Plus,
  LogIn as LogInIcon,
  LogOut,
  Crown,
  Settings,
  UserMinus,
  Loader2,
  AlertCircle,
  Zap,
  Shield,
} from "lucide-react";
import { useApi, useApiMutation } from "@/hooks/useApi";
import { useAuthStore } from "@/stores/auth";
import { toast } from "sonner";
import { UserLink } from "@/components/ui/user-link";

interface Group {
  group_id: string;
  name: string;
  description: string;
  tag: string;
  founder_id: number;
  member_count: number;
  total_hashrate: number;
}

interface GroupMember {
  user_id: number;
  username: string;
  total_hashrate: number;
  rig_count: number;
  blocks_mined: number;
}

interface GroupDetail extends Group {
  members: GroupMember[];
}

interface GroupResult {
  success: boolean;
  message?: string;
  group_id?: string;
}

function fmtDate(ts?: string | null): string {
  if (!ts) return "--";
  const d = new Date(ts);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

export default function GroupsPage() {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const userId = useAuthStore((s) => s.user?.id);

  const { data: groups, loading: groupsLoading, error: groupsError, refetch: refetchGroups } =
    useApi<Group[]>(isAuthenticated ? "/groups" : null);

  const [selectedGroupId, setSelectedGroupId] = useState<string | null>(null);
  const { data: groupDetail, loading: detailLoading, refetch: refetchDetail } =
    useApi<GroupDetail>(selectedGroupId ? `/groups/${selectedGroupId}` : null, [selectedGroupId]);

  // Mutations
  const { mutate: createGroup, loading: createLoading, error: createError } =
    useApiMutation<GroupResult>("/groups");

  // Dynamic-path mutations use raw fetch
  const token = useAuthStore((s) => s.token);
  const [joinLoading, setJoinLoading] = useState(false);
  const [leaveLoading, setLeaveLoading] = useState(false);
  const [kickLoading, setKickLoading] = useState(false);
  const [settingsLoading, setSettingsLoading] = useState(false);

  const apiCall = useCallback(async (path: string, method: string = "POST", body?: unknown) => {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (token) headers.Authorization = `Bearer ${token}`;
    const res = await fetch(`/api/v2${path}`, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    return res.json();
  }, [token]);

  // Dialog states
  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [createName, setCreateName] = useState("");
  const [createType, setCreateType] = useState("general");
  const [createDescription, setCreateDescription] = useState("");

  const [settingsDialogOpen, setSettingsDialogOpen] = useState(false);
  const [editName, setEditName] = useState("");
  const [editDescription, setEditDescription] = useState("");

  const [kickUserId, setKickUserId] = useState<string | null>(null);

  const handleCreate = useCallback(async () => {
    if (!createName.trim()) return;
    const result = await createGroup({
      name: createName.trim(),
      private: createType === "private",
    });
    if (result) {
      toast.success("Group created");
      setCreateDialogOpen(false);
      setCreateName("");
      setCreateType("public");
      setCreateDescription("");
      refetchGroups();
    }
  }, [createName, createType, createDescription, createGroup, refetchGroups]);

  const handleJoin = useCallback(async (groupId: string) => {
    setJoinLoading(true);
    try {
      const result = await apiCall(`/groups/${groupId}/join`);
      toast.success(result.message || "Joined group");
      refetchGroups();
      if (selectedGroupId === groupId) refetchDetail();
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : "Failed to join");
    } finally {
      setJoinLoading(false);
    }
  }, [apiCall, refetchGroups, selectedGroupId, refetchDetail]);

  const handleLeave = useCallback(async (groupId: string) => {
    setLeaveLoading(true);
    try {
      const result = await apiCall(`/groups/${groupId}/leave`);
      toast.success(result.message || "Left group");
      refetchGroups();
      if (selectedGroupId === groupId) refetchDetail();
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : "Failed to leave");
    } finally {
      setLeaveLoading(false);
    }
  }, [apiCall, refetchGroups, selectedGroupId, refetchDetail]);

  const handleKick = useCallback(async () => {
    if (!kickUserId || !selectedGroupId) return;
    setKickLoading(true);
    try {
      const result = await apiCall(`/groups/${selectedGroupId}/kick`, "POST", { user_id: kickUserId });
      toast.success(result.message || "Member kicked");
      setKickUserId(null);
      refetchDetail();
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : "Failed to kick");
    } finally {
      setKickLoading(false);
    }
  }, [kickUserId, selectedGroupId, apiCall, refetchDetail]);

  const handleSaveSettings = useCallback(async () => {
    if (!selectedGroupId) return;
    setSettingsLoading(true);
    try {
      const result = await apiCall(`/groups/${selectedGroupId}`, "PUT", {
        description: editDescription.trim() || undefined,
        tag: editName.trim() || undefined,
      });
      toast.success("Settings updated");
      setSettingsDialogOpen(false);
      refetchDetail();
      refetchGroups();
    } catch (e: unknown) {
      toast.error(e instanceof Error ? e.message : "Failed to update");
    } finally {
      setSettingsLoading(false);
    }
  }, [selectedGroupId, editName, editDescription, apiCall, refetchDetail, refetchGroups]);

  const isFounder = groupDetail && userId && String(groupDetail.founder_id) === String(userId);
  const isMember = groupDetail?.members?.some((m) => String(m.user_id) === String(userId));

  if (!isAuthenticated) {
    return (
      <div className="space-y-6">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Groups</h1>
          <p className="text-sm text-muted-foreground">Join or create groups with other users</p>
        </div>
        <Card>
          <CardContent className="flex flex-col items-center justify-center gap-3 py-16">
            <LogInIcon className="size-10 text-muted-foreground" />
            <p className="text-lg font-medium">Log in to view groups</p>
            <p className="text-sm text-muted-foreground">Sign in with Discord to browse and join groups.</p>
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Groups</h1>
          <p className="text-sm text-muted-foreground">Join or create groups with other users</p>
        </div>
        <Button className="gap-2" onClick={() => setCreateDialogOpen(true)}>
          <Plus className="size-4" />
          Create Group
        </Button>
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        {/* Groups list */}
        <div className="lg:col-span-2 space-y-4">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Users className="size-4" />
                All Groups
              </CardTitle>
            </CardHeader>
            <CardContent>
              {groupsLoading ? (
                <div className="space-y-3">
                  {[0, 1, 2, 3].map((i) => (
                    <Skeleton key={i} className="h-16 w-full" />
                  ))}
                </div>
              ) : groupsError ? (
                <div className="flex items-center gap-2 text-sm text-destructive">
                  <AlertCircle className="size-4" />{groupsError}
                </div>
              ) : groups && groups.length > 0 ? (
                <div className="space-y-2">
                  {groups.map((group) => (
                    <div
                      key={group.group_id}
                      className={`flex items-center justify-between rounded-lg border p-4 cursor-pointer transition-colors ${
                        selectedGroupId === group.group_id ? "border-primary bg-primary/5" : "border-border hover:bg-muted/50"
                      }`}
                      onClick={() => setSelectedGroupId(group.group_id)}
                    >
                      <div className="space-y-1">
                        <div className="flex items-center gap-2">
                          <span className="font-semibold">{group.name}</span>
                          {group.tag && <Badge variant="outline">{group.tag}</Badge>}
                        </div>
                        <div className="flex items-center gap-3 text-xs text-muted-foreground">
                          <span className="flex items-center gap-1">
                            <Users className="size-3" />{group.member_count} members
                          </span>
                          <span className="flex items-center gap-1">Founder: <UserLink userId={String(group.founder_id)} /></span>
                        </div>
                      </div>
                      <div className="flex gap-2">
                        <Button
                          size="sm"
                          variant="outline"
                          className="gap-1"
                          onClick={(e) => { e.stopPropagation(); handleJoin(group.group_id); }}
                          disabled={joinLoading}
                        >
                          <LogInIcon className="size-3" />Join
                        </Button>
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="flex h-32 items-center justify-center rounded-lg border border-dashed border-border">
                  <p className="text-sm text-muted-foreground">No groups yet. Create the first one!</p>
                </div>
              )}
            </CardContent>
          </Card>
        </div>

        {/* Group detail panel */}
        <div className="space-y-4">
          {selectedGroupId ? (
            detailLoading ? (
              <Card>
                <CardContent className="pt-6 space-y-3">
                  <Skeleton className="h-6 w-32" />
                  <Skeleton className="h-4 w-full" />
                  <Skeleton className="h-20 w-full" />
                </CardContent>
              </Card>
            ) : groupDetail ? (
              <>
                <Card>
                  <CardHeader>
                    <div className="flex items-center justify-between">
                      <CardTitle>{groupDetail.name}</CardTitle>
                      {isFounder && (
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => {
                            setEditName(groupDetail.tag || "");
                            setEditDescription(groupDetail.description || "");
                            setSettingsDialogOpen(true);
                          }}
                        >
                          <Settings className="size-4" />
                        </Button>
                      )}
                    </div>
                  </CardHeader>
                  <CardContent className="space-y-3">
                    {groupDetail.description && (
                      <p className="text-sm text-muted-foreground">{groupDetail.description}</p>
                    )}
                    <div className="grid grid-cols-2 gap-2 text-sm">
                      {groupDetail.tag && (
                        <div>
                          <span className="text-xs text-muted-foreground block">Tag</span>
                          <Badge variant="outline">{groupDetail.tag}</Badge>
                        </div>
                      )}
                      <div>
                        <span className="text-xs text-muted-foreground block">Members</span>
                        <span className="font-medium">{groupDetail.member_count}</span>
                      </div>
                      <div>
                        <span className="text-xs text-muted-foreground block">Total Hashrate</span>
                        <span className="font-medium">{groupDetail.total_hashrate.toLocaleString()} H/s</span>
                      </div>
                    </div>

                    {isMember && (
                      <Button
                        size="sm"
                        variant="outline"
                        className="w-full gap-1"
                        onClick={() => handleLeave(groupDetail.group_id)}
                        disabled={leaveLoading}
                      >
                        {leaveLoading ? <Loader2 className="size-3 animate-spin" /> : <LogOut className="size-3" />}
                        Leave Group
                      </Button>
                    )}
                    {!isMember && (
                      <Button
                        size="sm"
                        className="w-full gap-1"
                        onClick={() => handleJoin(groupDetail.group_id)}
                        disabled={joinLoading}
                      >
                        {joinLoading ? <Loader2 className="size-3 animate-spin" /> : <LogInIcon className="size-3" />}
                        Join Group
                      </Button>
                    )}
                  </CardContent>
                </Card>

                {/* Members */}
                <Card>
                  <CardHeader>
                    <CardTitle className="text-sm">Members</CardTitle>
                  </CardHeader>
                  <CardContent>
                    {groupDetail.members && groupDetail.members.length > 0 ? (
                      <div className="space-y-2">
                        {groupDetail.members.map((m) => (
                          <div key={m.user_id} className="flex items-center justify-between rounded-lg bg-muted/50 px-3 py-2">
                            <div className="flex items-center gap-2">
                              {m.user_id === groupDetail.founder_id && <Crown className="size-3 text-yellow-500" />}
                              <UserLink userId={String(m.user_id)} username={m.username} />
                              <Badge variant="outline" className="text-xs">{m.total_hashrate.toLocaleString()} H/s</Badge>
                            </div>
                            {isFounder && String(m.user_id) !== String(userId) && (
                              <Button
                                size="sm"
                                variant="ghost"
                                className="h-6 w-6 p-0"
                                onClick={() => setKickUserId(String(m.user_id))}
                              >
                                <UserMinus className="size-3 text-muted-foreground" />
                              </Button>
                            )}
                          </div>
                        ))}
                      </div>
                    ) : (
                      <p className="text-sm text-muted-foreground">No members</p>
                    )}
                  </CardContent>
                </Card>

              </>
            ) : (
              <Card>
                <CardContent className="py-8 text-center">
                  <p className="text-sm text-muted-foreground">Group not found</p>
                </CardContent>
              </Card>
            )
          ) : (
            <Card>
              <CardContent className="py-12 text-center">
                <Users className="mx-auto size-8 text-muted-foreground mb-2" />
                <p className="text-sm text-muted-foreground">Select a group to view details</p>
              </CardContent>
            </Card>
          )}
        </div>
      </div>

      {/* Create Group Dialog */}
      <Dialog open={createDialogOpen} onOpenChange={setCreateDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Create a Group</DialogTitle>
            <DialogDescription>Start a new group and invite others to join.</DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-2">
            <div className="space-y-2">
              <label className="text-sm font-medium">Group Name</label>
              <Input
                placeholder="My Group"
                value={createName}
                onChange={(e) => setCreateName(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <label className="text-sm font-medium">Type</label>
              <Select value={createType} onValueChange={(v) => setCreateType(v ?? "public")}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="public">Public</SelectItem>
                  <SelectItem value="private">Private (invite only)</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <label className="text-sm font-medium">Description (optional)</label>
              <Input
                placeholder="What is your group about?"
                value={createDescription}
                onChange={(e) => setCreateDescription(e.target.value)}
              />
            </div>
            {createError && (
              <div className="flex items-center gap-2 text-sm text-destructive">
                <AlertCircle className="size-4" />{createError}
              </div>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setCreateDialogOpen(false)}>Cancel</Button>
            <Button disabled={createLoading || !createName.trim()} onClick={handleCreate}>
              {createLoading ? (
                <><Loader2 className="size-4 animate-spin" />Creating...</>
              ) : (
                <><Plus className="size-4" />Create</>
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Kick Confirmation Dialog */}
      <Dialog open={!!kickUserId} onOpenChange={(open) => !open && setKickUserId(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Kick Member</DialogTitle>
            <DialogDescription>Are you sure you want to remove this member from the group?</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setKickUserId(null)}>Cancel</Button>
            <Button variant="destructive" disabled={kickLoading} onClick={handleKick}>
              {kickLoading ? <Loader2 className="size-4 animate-spin" /> : "Kick"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Settings Dialog */}
      <Dialog open={settingsDialogOpen} onOpenChange={setSettingsDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Group Settings</DialogTitle>
            <DialogDescription>Update your group configuration.</DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-2">
            <div className="space-y-2">
              <label className="text-sm font-medium">Tag (max 5 chars)</label>
              <Input value={editName} onChange={(e) => setEditName(e.target.value)} maxLength={5} />
            </div>
            <div className="space-y-2">
              <label className="text-sm font-medium">Description</label>
              <Input value={editDescription} onChange={(e) => setEditDescription(e.target.value)} />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setSettingsDialogOpen(false)}>Cancel</Button>
            <Button disabled={settingsLoading} onClick={handleSaveSettings}>
              {settingsLoading ? <Loader2 className="size-4 animate-spin" /> : "Save"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
