import { AssetsTabs, type AssetsTab } from "@/components/assets/assets-tabs";

export default async function AssetsPage({ searchParams }: PageProps<"/assets">) {
  const params = await searchParams;
  const requested = Array.isArray(params.tab) ? params.tab[0] : params.tab;
  const tab: AssetsTab = requested === "holdings" ? "holdings" : "pool";
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">股票与持仓</h1>
      <AssetsTabs tab={tab} />
    </div>
  );
}
