"""Read-only portfolio view for notification risk checks from a Web snapshot."""
from trading.positions import TradeGate


class SnapshotTradeGate(TradeGate):
    def __init__(self, store, config, snapshot):
        super().__init__(store, config)
        self.snapshot = snapshot

    def list_positions(self):
        return [{"symbol": row["symbol"], "name": row.get("name", ""),
                 "quantity": int(row.get("shares", 0)), "avg_cost": float(row.get("cost_price", 0)),
                 "opened_on": row.get("opened_on"), "updated_at": row.get("updated_at")}
                for row in self.snapshot.get("holdings", []) if float(row.get("shares", 0)) > 0]

    def get_position(self, symbol):
        return next((row for row in self.list_positions() if row["symbol"] == symbol), None)

    def trades_today(self, trade_day=None):
        from utils.time_utils import now_shanghai
        day = trade_day or now_shanghai().date().isoformat()
        return [row for row in self.snapshot.get("trades", []) if str(row.get("traded_at", ""))[:10] == day]
