from datetime import datetime
from sqlalchemy import BigInteger, Boolean, Column, DateTime, ForeignKey, Integer, String, Numeric, Text
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    
    id = Column(BigInteger, primary_key=True, index=True)
    username = Column(String(255), nullable=True)
    full_name = Column(String(255), nullable=True)
    is_admin = Column(Boolean, default=False, index=True)
    is_blocked = Column(Boolean, default=False, index=True)
    is_suspicious = Column(Boolean, default=False)
    warning_count = Column(Integer, default=0)
    referred_by = Column(BigInteger, nullable=True, index=True)
    referral_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    transactions = relationship("Transaction", back_populates="user")
    bought_coupons = relationship("Coupon", back_populates="buyer")

class Category(Base):
    __tablename__ = 'categories'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    terms = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True, index=True)
    
    coupons = relationship("Coupon", back_populates="category")

class Coupon(Base):
    __tablename__ = 'coupons'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    category_id = Column(Integer, ForeignKey('categories.id'), index=True)
    code = Column(String(255), nullable=False)
    price_inr = Column(Numeric(10, 2), nullable=False)
    is_sold = Column(Boolean, default=False, index=True)
    sold_to = Column(BigInteger, ForeignKey('users.id'), nullable=True, index=True)
    transaction_id = Column(Integer, ForeignKey('transactions.id'), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    category = relationship("Category", back_populates="coupons")
    buyer = relationship("User", back_populates="bought_coupons")
    transaction = relationship("Transaction", back_populates="coupons")

class Transaction(Base):
    __tablename__ = 'transactions'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey('users.id'), index=True)
    quantity = Column(Integer, default=1)
    amount = Column(Numeric(10, 2), nullable=False)
    payment_proof_id = Column(String(255), nullable=True)
    utr = Column(String(50), nullable=True, unique=True, index=True)
    provider_payment_charge_id = Column(String(255), nullable=True, index=True)
    status = Column(String(50), default='pending', index=True) # pending, completed, failed, cancelled
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    expires_at = Column(DateTime, nullable=True, index=True)
    
    user = relationship("User", back_populates="transactions")
    coupons = relationship("Coupon", back_populates="transaction")

class Channel(Base):
    __tablename__ = 'channels'
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    chat_id = Column(String(255), nullable=False) # @channel or -100...
    invite_link = Column(String(255), nullable=False)

class Setting(Base):
    __tablename__ = 'settings'
    key = Column(String(255), primary_key=True)
    value = Column(String(255), nullable=True)

class SupportContact(Base):
    __tablename__ = 'support_contacts'
    id = Column(Integer, primary_key=True, autoincrement=True)
    label = Column(String(255), nullable=False) # e.g. "Support #1"
    username = Column(String(255), nullable=False) # e.g. "helpdesk_coupon_bot"
    is_active = Column(Boolean, default=True)

class ReferralReward(Base):
    __tablename__ = 'referral_rewards'
    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(255), nullable=False)
    is_used = Column(Boolean, default=False)
    used_by = Column(BigInteger, ForeignKey('users.id'), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
