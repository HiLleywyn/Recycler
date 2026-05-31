import ProfilePageClient from "./client";

export async function generateStaticParams() {
  // At least one entry required for static export; FastAPI SPA fallback
  // serves the page for all other user IDs at runtime.
  return [{ userId: "_" }];
}

export default function ProfilePage() {
  return <ProfilePageClient />;
}
