from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping


WRITE_KEYWORD_RE = re.compile(r"\b(insert|update|delete|drop|alter|truncate|merge|call|execute|grant|revoke|create)\b", re.I)


@dataclass(frozen=True)
class RenderedSqlQuery:
    query_id: str
    sql: str
    params: dict[str, Any]
    max_rows: int
    timeout_sec: int


@dataclass(frozen=True)
class SqlTemplate:
    name: str
    purpose: str
    sql: str
    params: tuple[str, ...]
    max_rows: int
    timeout_sec: int

    def render(self, values: Mapping[str, Any]) -> RenderedSqlQuery:
        missing = [name for name in self.params if name not in values]
        if missing:
            raise ValueError(f"缺少 SQL 参数: {', '.join(missing)}")
        extra = sorted(set(values) - set(self.params))
        if extra:
            raise ValueError(f"未知 SQL 参数: {', '.join(extra)}")
        sql = validate_readonly_sql(self.sql)
        return RenderedSqlQuery(
            query_id=self.name,
            sql=sql,
            params={name: values[name] for name in self.params},
            max_rows=self.max_rows,
            timeout_sec=self.timeout_sec,
        )


def validate_readonly_sql(sql: str) -> str:
    normalized = sql.strip()
    if not normalized:
        raise ValueError("SQL 模板不能为空")
    if ";" in normalized.rstrip(";"):
        raise ValueError("SQL 模板只允许单条只读语句")
    lowered = normalized.lstrip().lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise ValueError("SQL 模板只允许 SELECT/WITH 只读查询")
    if WRITE_KEYWORD_RE.search(normalized):
        raise ValueError("SQL 模板只读校验失败，包含写入关键字")
    return normalized.rstrip(";")


SQL_TEMPLATES: dict[str, SqlTemplate] = {
    "overseas_invoice_attachment_fields": SqlTemplate(
        name="overseas_invoice_attachment_fields",
        purpose="按当前海外采购流程与精确明细身份核对上传发票空值",
        sql="""
            select m.requestid, d.id as detail_id, d.scfp
            from ecology.formtable_main_1295 m
            join ecology.formtable_main_1295_dt1 d on d.mainid = m.id
            where m.requestid = :request_id
              and d.id = :detail_id
              and rownum <= 2
        """,
        params=("request_id", "detail_id"),
        max_rows=2,
        timeout_sec=5,
    ),
    "tpm_small_marketing_fee_attachment_fields": SqlTemplate(
        name="tpm_small_marketing_fee_attachment_fields",
        purpose="按当前TPM流程与精确明细身份核对附件及发票引用空值",
        sql="""
            select m.requestid, d.id as detail_id, d.fj, d.glfp
            from ecology.formtable_main_912 m
            join ecology.formtable_main_912_dt1 d on d.mainid = m.id
            where m.requestid = :request_id
              and d.id = :detail_id
              and rownum <= 2
        """,
        params=("request_id", "detail_id"),
        max_rows=2,
        timeout_sec=5,
    ),
    "general_expense_attachment_fields": SqlTemplate(
        name="general_expense_attachment_fields",
        purpose="按当前一般费用流程与精确明细身份核对附件及发票引用空值",
        sql="""
            select m.requestid, d.id as detail_id, d.scfj, d.fj, d.glfp
            from ecology.formtable_main_569 m
            join ecology.formtable_main_569_dt1 d on d.mainid = m.id
            where m.requestid = :request_id
              and d.id = :detail_id
              and rownum <= 2
        """,
        params=("request_id", "detail_id"),
        max_rows=2,
        timeout_sec=5,
    ),
    "workflow_attachment_document": SqlTemplate(
        name="workflow_attachment_document",
        purpose="按当前流程与文件的明确共享关联定位唯一文档",
        sql="""
            select docid, imagefileid
            from (
                select distinct d.docid, d.imagefileid
                from ecology.docimagefile d
                where d.imagefileid = :file_id
                  and exists (
                      select 1 from ecology.workflow_docshareinfo s
                      where s.docid = d.docid
                        and s.requestid = :request_id
                  )
            )
            where rownum <= 2
        """,
        params=("request_id", "file_id"),
        max_rows=2,
        timeout_sec=5,
    ),
    "workflow_request_by_mark": SqlTemplate(
        name="workflow_request_by_mark",
        purpose="按业务流程编号精确定位唯一 OA requestId",
        sql="""
            select requestid,
                   requestmark
            from ecology.workflow_requestbase
            where requestmark = :request_mark
              and rownum <= 2
        """,
        params=("request_mark",),
        max_rows=2,
        timeout_sec=5,
    ),
    "travel_job_level": SqlTemplate(
        name="travel_job_level",
        purpose="差旅出差住宿天数校验：按报销人职级ID查询职务类别和职务名称",
        sql="""
            select id,
                   zwlb,
                   zwmc,
                   sfqy
            from ecology.uf_zwjbnew
            where id = :job_level_id
              and rownum <= 2
        """,
        params=("job_level_id",),
        max_rows=2,
        timeout_sec=5,
    ),
    "travel_responsible_regions": SqlTemplate(
        name="travel_responsible_regions",
        purpose="差旅跨区域带车校验：查询人员备案/费用报销人员信息负责区域 fzqy",
        sql="""
            select source_table,
                   requestid,
                   lcbh,
                   sqr,
                   sqrq,
                   fzqy
            from (
                select source_table,
                       requestid,
                       lcbh,
                       sqr,
                       sqrq,
                       fzqy
                from (
                    select 'formtable_main_589' as source_table,
                           requestid,
                           lcbh,
                           sqr,
                           sqrq,
                           fzqy
                    from ecology.formtable_main_589
                    where fzqy is not null
                      and sqr = :person_id
                    union all
                    select 'formtable_main_941' as source_table,
                           requestid,
                           lcbh,
                           sqr,
                           sqrq,
                           fzqy
                    from ecology.formtable_main_941
                    where fzqy is not null
                      and sqr = :person_id
                )
                order by sqrq desc
            )
            where rownum <= 20
        """,
        params=("person_id",),
        max_rows=20,
        timeout_sec=5,
    ),
}


def render_query(name: str, params: Mapping[str, Any]) -> RenderedSqlQuery:
    template = SQL_TEMPLATES.get(name)
    if template is None:
        raise ValueError(f"未知 SQL 模板: {name}")
    return template.render(params)
