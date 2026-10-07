"use client";

import { useRouter } from "next/navigation";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { HoldingsPanel } from "@/components/assets/holdings-panel";
import { PoolPanel } from "@/components/assets/pool-panel";

export type AssetsTab = "pool" | "holdings";

/** Tabs are URL-driven so refresh, back and forward keep the selected panel. */
export function AssetsTabs({ tab }: { tab: AssetsTab }) {
  const router = useRouter();
  return (
    <Tabs value={tab} onValueChange={(value) => router.push(`/assets?tab=${value}`, { scroll: false })}>
      <TabsList variant="line">
        <TabsTrigger value="pool">股票池</TabsTrigger>
        <TabsTrigger value="holdings">我的持仓</TabsTrigger>
      </TabsList>
      <TabsContent value="pool"><PoolPanel /></TabsContent>
      <TabsContent value="holdings"><HoldingsPanel /></TabsContent>
    </Tabs>
  );
}
