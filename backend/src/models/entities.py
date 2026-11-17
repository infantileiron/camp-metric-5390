from sqlalchemy import Column,Integer,String,Float,JSON,Text,Boolean,ForeignKey,UniqueConstraint
from src.config.database import Base
class User(Base):
 __tablename__='users'
 id=Column(Integer,primary_key=True);email=Column(String(100),unique=True,nullable=False);password=Column(String(200));role=Column(String(20))
class Metric(Base):
 __tablename__='metrics';__table_args__=(UniqueConstraint('code','version'),)
 id=Column(Integer,primary_key=True);code=Column(String(40),nullable=False);version=Column(Integer,nullable=False);name=Column(String(100));numerator=Column(String(200));denominator=Column(String(200));numerator_mode=Column(String(20));denominator_mode=Column(String(20));note=Column(Text,default='');locked=Column(Boolean,default=False)
class Fact(Base):
 __tablename__='facts';__table_args__=(UniqueConstraint('class_code','day'),)
 id=Column(Integer,primary_key=True);class_code=Column(String(50),nullable=False);day=Column(String(10),nullable=False);mode=Column(String(20));enrolled=Column(Integer);attended=Column(Integer);completed=Column(Integer);refunds=Column(Integer);note=Column(Text,default='')
class Snapshot(Base):
 __tablename__='snapshots'
 id=Column(Integer,primary_key=True);metric_id=Column(Integer,ForeignKey('metrics.id'));baseline_id=Column(Integer,ForeignKey('snapshots.id'),nullable=True);date_from=Column(String(10));date_to=Column(String(10));state=Column(String(20),default='queued');result=Column(JSON,nullable=True);error=Column(Text,default='');note=Column(Text,default='');approval=Column(String(20),default='draft');approved_by=Column(String(100),default='')
class Audit(Base):
 __tablename__='audits'
 id=Column(Integer,primary_key=True);actor=Column(String(100));action=Column(String(200));created=Column(String(40))


class Publication(Base):
 """签核快照的公开发布包（台账头）。

 state: pending 待检 → publishing 公开中 → published 已公开 → withdrawn 已撤下。
 待检包不计入历史披露；公开中与已公开、已撤下都计入历史披露，草稿不在
 本表中。发布只按 check_result 里冻结的组合与合计记账，重试不重复。
 """
 __tablename__='publications';__table_args__=(UniqueConstraint('snapshot_id','idempotency_key'),)
 id=Column(Integer,primary_key=True);snapshot_id=Column(Integer,ForeignKey('snapshots.id'),nullable=False,index=True)
 state=Column(String(20),default='pending',nullable=False);note=Column(Text,default='')
 combos=Column(JSON,nullable=False);check_result=Column(JSON,nullable=True)
 check_history_fingerprint=Column(String(64),default='');check_package_fingerprint=Column(String(64),default='')
 checked_at=Column(String(40),default='');checked_by=Column(String(100),default='')
 created=Column(String(40),default='');created_by=Column(String(100),default='')
 published_at=Column(String(40),default='');published_by=Column(String(100),default='')
 withdrawn_at=Column(String(40),default='');withdrawn_by=Column(String(100),default='')
 idempotency_key=Column(String(100),nullable=True);revision=Column(Integer,default=1,nullable=False)


class PublicationItem(Base):
 """已公开的班级组合合计。同一快照同一组合只记一次，重试不得重复记账。

 class_codes 仅用于管理页复盘和泄露诊断，公开接口不返回。
 """
 __tablename__='publication_items';__table_args__=(UniqueConstraint('snapshot_id','combo_key'),)
 id=Column(Integer,primary_key=True);publication_id=Column(Integer,ForeignKey('publications.id'),nullable=False,index=True)
 snapshot_id=Column(Integer,ForeignKey('snapshots.id'),nullable=False,index=True)
 combo_key=Column(String(1000),nullable=False);label=Column(String(100),nullable=False)
 class_codes=Column(JSON,nullable=False);total_completed=Column(Integer,nullable=False)
 created=Column(String(40),default='')
