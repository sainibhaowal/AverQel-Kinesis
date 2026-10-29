import { redirect } from "next/navigation";

export default function Home(): React.ReactNode {
  redirect("/dashboard/deepspace");
  return null;
}
