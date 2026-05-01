"""User & portfolio management with PostgreSQL."""

import os
import secrets
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import (
    create_engine, Column, String, Boolean, DateTime, Text,
    Integer, Float, ForeignKey, JSON, Index
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://forex:CHANGE_ME@localhost:5432/forex_trading"
)

Base = declarative_base()


# ── Models ──

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, autoincrement=True)
    api_key = Column(String(100), unique=True, nullable=False, index=True)
    username = Column(String(50), unique=True, nullable=False)
    display_name = Column(String(100), default="")
    is_admin = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    settings = Column(JSON, default=dict)  # Custom risk params per user

    portfolio = relationship("PortfolioRecord", back_populates="user", uselist=False)
    orders = relationship("OrderRecord", back_populates="user", order_by="OrderRecord.created_at.desc()")
    trades = relationship("TradeRecord", back_populates="user", order_by="TradeRecord.created_at.desc()")


class PortfolioRecord(Base):
    __tablename__ = "portfolios"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    cash = Column(Float, default=100_000.0)
    total_realized_pnl = Column(Float, default=0.0)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="portfolio")
    positions = relationship("PositionRecord", back_populates="portfolio", cascade="all, delete-orphan")


class PositionRecord(Base):
    __tablename__ = "positions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    portfolio_id = Column(Integer, ForeignKey("portfolios.id"), nullable=False)
    symbol = Column(String(30), nullable=False)
    quantity = Column(Integer, default=0)
    avg_price = Column(Float, default=0.0)
    entry_time = Column(DateTime, default=datetime.utcnow)
    highest_price = Column(Float, default=0.0)
    signal_confidence = Column(Float, default=0.0)
    ai_stop_loss = Column(Float, nullable=True)
    ai_target = Column(Float, nullable=True)
    dynamic_stop_loss_pct = Column(Float, default=0.005)
    dynamic_take_profit_pct = Column(Float, default=0.01)

    portfolio = relationship("PortfolioRecord", back_populates="positions")

    __table_args__ = (Index("ix_positions_portfolio_symbol", "portfolio_id", "symbol", unique=True),)


class OrderRecord(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    symbol = Column(String(30), nullable=False)
    side = Column(String(4), nullable=False)  # BUY or SELL
    quantity = Column(Integer, nullable=False)
    price = Column(Float, nullable=False)
    fill_price = Column(Float, nullable=False)
    slippage = Column(Float, default=0.0)
    brokerage = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="orders")


class TradeRecord(Base):
    """Closed trade (sell) with P&L."""
    __tablename__ = "trades"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    symbol = Column(String(30), nullable=False)
    quantity = Column(Integer, nullable=False)
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=False)
    pnl = Column(Float, default=0.0)
    pnl_pct = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="trades")


# ── Engine & Session ──

# SQLite uses SingletonThreadPool which rejects pool_size/max_overflow; only
# pass those tunables to real connection-pool dialects (Postgres, MySQL).
_pool_kwargs = {} if DATABASE_URL.startswith("sqlite") else {"pool_size": 10, "max_overflow": 20}
engine = create_engine(DATABASE_URL, pool_pre_ping=True, **_pool_kwargs)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)


# ── User CRUD ──

def _ensure_admin():
    with Session() as db:
        admin = db.query(User).filter(User.is_admin == True).first()
        if not admin:
            key = os.environ.get("ADMIN_API_KEY", "forex-admin-" + secrets.token_hex(16))
            admin = User(
                api_key=key,
                username="admin",
                display_name="Admin",
                is_admin=True,
            )
            db.add(admin)
            db.flush()
            # Create admin portfolio
            portfolio = PortfolioRecord(user_id=admin.id)
            db.add(portfolio)
            db.commit()
            # Don't log the key — write it to a file with restrictive perms so
            # log aggregators (Docker, journald, Datadog) never capture it.
            key_path = os.environ.get("ADMIN_KEY_FILE", "/run/forex/admin.key")
            try:
                Path(key_path).parent.mkdir(parents=True, exist_ok=True)
                Path(key_path).write_text(key)
                os.chmod(key_path, 0o600)
                print(f"[USERS] Admin user created. API key written to {key_path} (mode 0600).")
            except OSError as e:
                # Fallback: write to CWD if /run/forex isn't writable (e.g. local dev).
                fallback = Path("./admin.key").resolve()
                fallback.write_text(key)
                os.chmod(fallback, 0o600)
                print(f"[USERS] Admin user created. API key written to {fallback} (mode 0600). ({e})")
            return key
        return admin.api_key


def get_user(api_key: str) -> User | None:
    with Session() as db:
        user = db.query(User).filter(User.api_key == api_key, User.is_active == True).first()
        if user:
            db.expunge(user)
        return user


def get_user_by_username(username: str) -> User | None:
    with Session() as db:
        user = db.query(User).filter(User.username == username).first()
        if user:
            db.expunge(user)
        return user


def create_user(username: str, display_name: str = "", is_admin: bool = False) -> tuple[dict, str]:
    api_key = "fx-" + secrets.token_hex(20)
    with Session() as db:
        user = User(
            api_key=api_key,
            username=username.lower().strip(),
            display_name=display_name or username,
            is_admin=is_admin,
        )
        db.add(user)
        db.flush()
        portfolio = PortfolioRecord(user_id=user.id)
        db.add(portfolio)
        db.commit()
        return {"username": user.username, "display_name": user.display_name, "id": user.id}, api_key


def list_users() -> list[dict]:
    with Session() as db:
        users = db.query(User).all()
        return [
            {
                "username": u.username,
                "display_name": u.display_name,
                "is_admin": u.is_admin,
                "is_active": u.is_active,
                "created_at": u.created_at.isoformat() if u.created_at else None,
                "api_key_preview": u.api_key[:10] + "...",
            }
            for u in users
        ]


def deactivate_user(username: str) -> bool:
    with Session() as db:
        user = db.query(User).filter(User.username == username).first()
        if user:
            user.is_active = False
            db.commit()
            return True
        return False


def regenerate_key(username: str) -> str | None:
    with Session() as db:
        user = db.query(User).filter(User.username == username).first()
        if user:
            new_key = "fx-" + secrets.token_hex(20)
            user.api_key = new_key
            db.commit()
            return new_key
        return None


# ── Portfolio operations (all in PostgreSQL) ──

def get_portfolio(user: User) -> dict:
    """Get user's portfolio as a dict."""
    with Session() as db:
        portfolio = db.query(PortfolioRecord).filter(PortfolioRecord.user_id == user.id).first()
        if not portfolio:
            portfolio = PortfolioRecord(user_id=user.id)
            db.add(portfolio)
            db.commit()
            db.refresh(portfolio)

        positions = {}
        for pos in portfolio.positions:
            positions[pos.symbol] = {
                "symbol": pos.symbol,
                "quantity": pos.quantity,
                "avg_price": pos.avg_price,
                "entry_time": pos.entry_time.isoformat() if pos.entry_time else None,
                "highest_price": pos.highest_price,
                "signal_confidence": pos.signal_confidence,
                "ai_stop_loss": pos.ai_stop_loss,
                "ai_target": pos.ai_target,
                "dynamic_stop_loss_pct": pos.dynamic_stop_loss_pct,
                "dynamic_take_profit_pct": pos.dynamic_take_profit_pct,
            }

        return {
            "cash": portfolio.cash,
            "positions": positions,
            "total_realized_pnl": portfolio.total_realized_pnl,
        }


def execute_buy(user: User, symbol: str, price: float, quantity: int,
                fill_price: float, slippage: float, total_cost: float,
                confidence: float = 0.0, ai_signal: dict | None = None,
                dynamic_sl: float = 0.005, dynamic_tp: float = 0.01) -> dict | None:
    """Execute a buy order in PostgreSQL. Returns order dict or None."""
    with Session() as db:
        portfolio = db.query(PortfolioRecord).filter(PortfolioRecord.user_id == user.id).first()
        if not portfolio or portfolio.cash < total_cost:
            return None

        portfolio.cash -= total_cost

        # Upsert position
        pos = db.query(PositionRecord).filter(
            PositionRecord.portfolio_id == portfolio.id,
            PositionRecord.symbol == symbol
        ).first()

        if pos:
            total_qty = pos.quantity + quantity
            pos.avg_price = (pos.avg_price * pos.quantity + fill_price * quantity) / total_qty
            pos.quantity = total_qty
            pos.highest_price = max(pos.highest_price, fill_price)
            pos.signal_confidence = max(pos.signal_confidence, confidence)
            pos.dynamic_stop_loss_pct = dynamic_sl
            pos.dynamic_take_profit_pct = dynamic_tp
            if ai_signal:
                if ai_signal.get("stop_loss"):
                    pos.ai_stop_loss = float(ai_signal["stop_loss"])
                if ai_signal.get("target"):
                    pos.ai_target = float(ai_signal["target"])
        else:
            pos = PositionRecord(
                portfolio_id=portfolio.id,
                symbol=symbol,
                quantity=quantity,
                avg_price=fill_price,
                highest_price=fill_price,
                signal_confidence=confidence,
                dynamic_stop_loss_pct=dynamic_sl,
                dynamic_take_profit_pct=dynamic_tp,
                ai_stop_loss=float(ai_signal["stop_loss"]) if ai_signal and ai_signal.get("stop_loss") else None,
                ai_target=float(ai_signal["target"]) if ai_signal and ai_signal.get("target") else None,
            )
            db.add(pos)

        order = OrderRecord(
            user_id=user.id,
            symbol=symbol,
            side="BUY",
            quantity=quantity,
            price=price,
            fill_price=fill_price,
            slippage=slippage,
        )
        db.add(order)
        db.commit()

        return {"side": "BUY", "symbol": symbol, "quantity": quantity, "fill_price": fill_price}


def execute_sell(user: User, symbol: str, price: float, quantity: int | None = None) -> dict | None:
    """Execute a sell order in PostgreSQL. Returns trade dict or None."""
    import random
    import config

    with Session() as db:
        portfolio = db.query(PortfolioRecord).filter(PortfolioRecord.user_id == user.id).first()
        if not portfolio:
            return None

        pos = db.query(PositionRecord).filter(
            PositionRecord.portfolio_id == portfolio.id,
            PositionRecord.symbol == symbol
        ).first()
        if not pos:
            return None

        sell_qty = min(quantity or pos.quantity, pos.quantity)
        if sell_qty <= 0:
            return None

        slippage = config.SLIPPAGE_PCT * random.uniform(0.5, 2.0)
        fill_price = price * (1 - slippage)
        proceeds = sell_qty * fill_price

        pnl = (fill_price - pos.avg_price) * sell_qty
        pnl_pct = ((fill_price - pos.avg_price) / pos.avg_price) * 100 if pos.avg_price > 0 else 0

        portfolio.cash += proceeds
        portfolio.total_realized_pnl += pnl

        # Record trade
        trade = TradeRecord(
            user_id=user.id,
            symbol=symbol,
            quantity=sell_qty,
            entry_price=pos.avg_price,
            exit_price=fill_price,
            pnl=round(pnl, 2),
            pnl_pct=round(pnl_pct, 2),
        )
        db.add(trade)

        # Record order
        order = OrderRecord(
            user_id=user.id,
            symbol=symbol,
            side="SELL",
            quantity=sell_qty,
            price=price,
            fill_price=fill_price,
            slippage=slippage,
        )
        db.add(order)

        # Remove or reduce position
        if sell_qty >= pos.quantity:
            db.delete(pos)
        else:
            pos.quantity -= sell_qty

        db.commit()

        return {
            "side": "SELL", "symbol": symbol, "quantity": sell_qty,
            "fill_price": round(fill_price, 5), "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2),
        }


def get_user_trades(user: User, limit: int = 50) -> list[dict]:
    with Session() as db:
        trades = db.query(TradeRecord).filter(
            TradeRecord.user_id == user.id
        ).order_by(TradeRecord.created_at.desc()).limit(limit).all()
        return [
            {
                "symbol": t.symbol, "side": "SELL", "quantity": t.quantity,
                "entry_price": t.entry_price, "exit_price": t.exit_price,
                "pnl": t.pnl, "pnl_pct": t.pnl_pct,
                "timestamp": t.created_at.isoformat() if t.created_at else None,
            }
            for t in trades
        ]


def reset_portfolio(user: User, initial_capital: float = 100_000.0) -> None:
    """Reset user's portfolio to initial state."""
    with Session() as db:
        portfolio = db.query(PortfolioRecord).filter(PortfolioRecord.user_id == user.id).first()
        if portfolio:
            # Delete all positions
            db.query(PositionRecord).filter(PositionRecord.portfolio_id == portfolio.id).delete()
            portfolio.cash = initial_capital
            portfolio.total_realized_pnl = 0.0
        else:
            portfolio = PortfolioRecord(user_id=user.id, cash=initial_capital)
            db.add(portfolio)
        db.commit()


def get_portfolio_summary(user: User, prices: dict[str, float]) -> dict:
    """Get portfolio summary with live P&L."""
    with Session() as db:
        portfolio = db.query(PortfolioRecord).filter(PortfolioRecord.user_id == user.id).first()
        if not portfolio:
            return {
                "cash": 100_000.0, "positions_value": 0.0, "total_value": 100_000.0,
                "initial_capital": 100_000.0, "total_return_pct": 0.0,
                "realized_pnl": 0.0, "open_positions": 0, "total_trades": 0,
            }

        positions_value = 0.0
        for pos in portfolio.positions:
            current = prices.get(pos.symbol, pos.avg_price)
            positions_value += pos.quantity * current

        total_value = portfolio.cash + positions_value
        initial = 100_000.0
        total_trades = db.query(TradeRecord).filter(TradeRecord.user_id == user.id).count()

        return {
            "cash": round(portfolio.cash, 2),
            "positions_value": round(positions_value, 2),
            "total_value": round(total_value, 2),
            "initial_capital": initial,
            "total_return_pct": round(((total_value - initial) / initial) * 100, 2),
            "realized_pnl": round(portfolio.total_realized_pnl, 2),
            "open_positions": len(portfolio.positions),
            "total_trades": total_trades,
        }


def get_positions_detail(user: User, prices: dict[str, float]) -> dict:
    """Get detailed positions with live P&L."""
    with Session() as db:
        portfolio = db.query(PortfolioRecord).filter(PortfolioRecord.user_id == user.id).first()
        if not portfolio:
            return {}
        result = {}
        for pos in portfolio.positions:
            current = prices.get(pos.symbol, pos.avg_price)
            pnl = (current - pos.avg_price) * pos.quantity
            pnl_pct = ((current - pos.avg_price) / pos.avg_price) * 100 if pos.avg_price > 0 else 0
            result[pos.symbol] = {
                "symbol": pos.symbol,
                "quantity": pos.quantity,
                "avg_price": round(pos.avg_price, 5),
                "current_price": round(current, 5),
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 2),
                "highest_price": round(pos.highest_price, 5),
                "entry_time": pos.entry_time.isoformat() if pos.entry_time else "",
            }
        return result


# Auto-create admin on import
ADMIN_KEY = _ensure_admin()
