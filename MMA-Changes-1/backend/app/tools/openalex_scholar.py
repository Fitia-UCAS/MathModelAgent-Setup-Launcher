# app/tools/openalex_scholar.py

# 1 导入依赖
import requests
from typing import List, Dict, Any, Tuple
from app.services.redis_manager import redis_manager
from app.schemas.response import ScholarMessage


# 2 OpenAlexScholar
# 2.1 目的：封装 OpenAlex API 请求，支持论文检索、摘要解析、引用格式化等
class OpenAlexScholar:
    def __init__(self, task_id: str, email: str = None):
        self.base_url = "https://api.openalex.org"
        self.email = email
        self.task_id = task_id

    # 2.2 构造请求 URL
    def _get_request_url(self, endpoint: str) -> str:
        if endpoint.startswith("/"):
            endpoint = endpoint[1:]
        return f"{self.base_url}/{endpoint}"

    # 2.3 从 abstract_inverted_index 重建摘要
    def _get_abstract_from_index(self, abstract_inverted_index: Dict) -> str:
        if not abstract_inverted_index:
            return ""
        max_position = 0
        for positions in abstract_inverted_index.values():
            if positions and max(positions) > max_position:
                max_position = max(positions)
        words = [""] * (max_position + 1)
        for word, positions in abstract_inverted_index.items():
            for position in positions:
                words[position] = word
        return " ".join(words).strip()

    # 2.4 搜索论文
    async def search_papers(self, query: str, limit: int = 8) -> List[Dict[str, Any]]:
        base_url = self._get_request_url("works")
        params = {
            "search": query,
            "per_page": limit,
            "select": "id,title,display_name,authorships,cited_by_count,doi,publication_year,"
            "biblio,abstract_inverted_index,host_venue,primary_location",
        }
        if self.email:
            params["mailto"] = self.email
        else:
            raise ValueError("配置OpenAlex邮箱获取访问文献权利")

        headers = {"User-Agent": f"OpenAlexScholar/1.0 (mailto:{self.email})" if self.email else "OpenAlexScholar/1.0"}

        try:
            response = requests.get(base_url, params=params, headers=headers)
            response.raise_for_status()
            results = response.json()
        except requests.exceptions.HTTPError as e:
            if response.status_code == 403:
                print("提示: 403错误通常意味着需要提供有效邮箱或遵循 polite pool 规则")
            if hasattr(response, "text"):
                print(f"响应内容: {response.text}")
            raise
        except Exception as e:
            raise

        papers = []
        paper_titles = []
        for work in results.get("results", []):
            abstract = self._get_abstract_from_index(work.get("abstract_inverted_index", {}))

            authors = []
            for authorship in work.get("authorships", []):
                author = authorship.get("author", {})
                if author:
                    author_info = {
                        "name": author.get("display_name"),
                        "position": authorship.get("author_position"),
                        "institution": (
                            authorship.get("institutions", [{}])[0].get("display_name")
                            if authorship.get("institutions")
                            else None
                        ),
                    }
                    authors.append(author_info)

            biblio = work.get("biblio", {})
            citation = {
                "volume": biblio.get("volume"),
                "issue": biblio.get("issue"),
                "first_page": biblio.get("first_page"),
                "last_page": biblio.get("last_page"),
            }

            paper = {
                "title": work.get("display_name") or work.get("title", ""),
                "abstract": abstract,
                "authors": authors,
                "citations_count": work.get("cited_by_count"),
                "doi": work.get("doi"),
                "publication_year": work.get("publication_year"),
                "citation_info": citation,
                "host_venue": work.get("host_venue"),
                "primary_location": work.get("primary_location"),
                "citation_format": self._format_citation(work),
            }
            papers.append(paper)
            paper_titles.append(paper["title"])

        await redis_manager.publish_message(
            self.task_id,
            ScholarMessage(input={"query": query}, output=paper_titles),
        )
        return papers

    # 2.5 文献转字符串（便于直接展示）
    def papers_to_str(self, papers: List[Dict[str, Any]]) -> str:
        result = ""
        for paper in papers:
            result += "\n" + "=" * 100
            result += f"\n标题: {paper['title']}"
            result += f"\n摘要: {paper['abstract']}"
            result += "\n作者:"
            for author in paper["authors"]:
                result += f"- {author['name']}"
            result += f"\n引用次数: {paper['citations_count']}"
            result += f"\n发表年份: {paper['publication_year']}"
            result += f"\n引用格式:\n{paper['citation_format']}"
            result += "=" * 100
        return result

    # 2.6 格式化引用
    def _format_citation(self, work: Dict[str, Any]) -> str:
        authors = [
            authorship.get("author", {}).get("display_name")
            for authorship in work.get("authorships", [])
            if authorship.get("author")
        ]
        if len(authors) > 3:
            authors_str = f"{authors[0]} et al."
        else:
            authors_str = ", ".join(authors)
        title = work.get("display_name") or work.get("title", "")
        year = work.get("publication_year", "")
        doi = work.get("doi", "")
        citation = f"{authors_str} ({year}). {title}."
        if doi:
            citation += f" DOI: {doi}"
        return citation


# 3 辅助方法
# 3.1 将论文转为 (citation_text, url) tuple，便于 WriterResponse 使用
def paper_to_footnote_tuple(paper: Dict[str, Any]) -> Tuple[str, str]:
    title = str(paper.get("title") or "")
    year = str(paper.get("publication_year") or "")

    authors = paper.get("authors") or []
    author_names = [a.get("name") for a in authors if isinstance(a, dict) and a.get("name")]
    authors_str = ""
    if author_names:
        authors_str = "; ".join(author_names[:3])
        if len(author_names) > 3:
            authors_str += " et al."

    venue = ""
    host_venue = paper.get("host_venue")
    if isinstance(host_venue, dict):
        venue = host_venue.get("display_name") or ""

    citation_text = f"{title}"
    if authors_str:
        citation_text += f" — {authors_str}"
    if year:
        citation_text += f" ({year})"
    if venue:
        citation_text += f", {venue}"

    url = ""
    doi = paper.get("doi")
    if doi:
        url = f"https://doi.org/{doi.split('doi.org/')[-1]}"
    else:
        loc = paper.get("primary_location") or {}
        if isinstance(loc, dict):
            url = loc.get("landing_page_url") or loc.get("pdf_url") or ""

    return citation_text.strip(), url.strip()
