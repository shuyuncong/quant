import { notFound } from "next/navigation";
import Strategies from "../../strategies/page";
import Notifications from "../../notifications/page";
import Models from "../../models/page";
import Schedule from "../../schedule/page";
import Logs from "../../logs/page";
const pages = { strategies: Strategies, notifications: Notifications, models: Models, schedule: Schedule, logs: Logs };
export default async function SettingsSection({ params }: { params: Promise<{ section: string }> }) {
  const { section } = await params;
  const Page = pages[section as keyof typeof pages];
  if (!Page) notFound();
  return <Page />;
}
