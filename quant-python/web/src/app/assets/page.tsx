"use client";
import PoolPage from "../pool/page";
import HoldingsPage from "../holdings/page";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
export default function AssetsPage() {
  return <div className="space-y-4"><h1 className="text-xl font-semibold">股票与持仓</h1><Tabs defaultValue="pool"><TabsList variant="line"><TabsTrigger value="pool">股票池</TabsTrigger><TabsTrigger value="holdings">我的持仓</TabsTrigger></TabsList><TabsContent value="pool"><PoolPage /></TabsContent><TabsContent value="holdings"><HoldingsPage /></TabsContent></Tabs></div>;
}
