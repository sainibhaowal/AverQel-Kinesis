import { redirect } from "next/navigation";

export default function DashboardPage(): React.ReactNode {
  redirect("/dashboard/deepspace");
  return null;
}
