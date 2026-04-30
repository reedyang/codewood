import os
import sys
import json
import hashlib
import contextlib
import concurrent.futures
import gc
import logging
import threading
from pathlib import Path
from typing import List, Dict, Optional, Any, Iterator, Tuple
from datetime import datetime

# Knowledge base imports: ChromaDB has known issues on Python 3.14 (Pydantic v1 incompatibility).
# Catching any import/config error so the app can run without knowledge base on unsupported envs.
try:
    # 注意：不得在后台线程里对 sys.stdout 做 redirect_stdout：sys.stdout 进程全局，
    # 会与主线程 run() 的 print 竞态，导致启动提示等输出丢失。
    import chromadb
    from chromadb.config import Settings
    from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
    import sentence_transformers  # noqa: F401 — required for local embeddings (see requirements.txt)
    try:
        from langchain_text_splitters import RecursiveCharacterTextSplitter
    except ImportError:
        from langchain.text_splitter import RecursiveCharacterTextSplitter
    from langchain_community.document_loaders import (
        TextLoader,
        PDFMinerLoader,
        Docx2txtLoader,
        UnstructuredMarkdownLoader,
        UnstructuredCSVLoader,
        UnstructuredExcelLoader
    )
    KNOWLEDGE_AVAILABLE = True
except Exception as e:
    KNOWLEDGE_AVAILABLE = False
    msg = f"[Warning] Knowledge base unavailable: {e}"
    if sys.version_info >= (3, 14):
        msg += " (ChromaDB is not compatible with Python 3.14; use Python 3.12 or 3.13 for knowledge base.)"
    else:
        msg += " (If missing deps, run: pip install -r requirements.txt)"
        el = str(e).lower()
        if any(x in el for x in ("huggingface", "transformers", "sentence", "hf_api")):
            msg += (
                " | Hugging Face 栈损坏或版本过旧时常见；可尝试: "
                "pip install -U \"huggingface_hub>=0.26\" \"transformers>=4.41\" sentence-transformers"
            )
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", errors="replace").decode("ascii"))
    try:
        logging.getLogger("smartshell.knowledge").warning("%s", msg)
    except Exception:
        pass

# Local Sentence-Transformers model id (Hugging Face Hub); no Ollama or cloud LLM API required.
DEFAULT_LOCAL_EMBEDDING_MODEL = "all-MiniLM-L6-v2"

_kb_log = logging.getLogger("smartshell.knowledge")


@contextlib.contextmanager
def _suppress_pdfminer_font_warnings() -> Iterator[None]:
    """屏蔽 pdfminer 对部分 PDF 字体的 FontBBox 警告（不影响解析结果，仅减少控制台刷屏）。"""
    lg = logging.getLogger("pdfminer")
    prev = lg.level
    lg.setLevel(logging.ERROR)
    try:
        yield
    finally:
        lg.setLevel(prev)


@contextlib.contextmanager
def _quiet_hf_transformers_embedding_init() -> Iterator[None]:
    """
    仅在加载 SentenceTransformer 嵌入模型期间：抑制 HF Hub 匿名访问类 UserWarning、
    transformers 的 tqdm（如 Loading weights）与 LOAD REPORT 等控制台输出；结束后恢复原设置。
    """
    import warnings

    tr_logging = None
    prev_verbosity: Optional[int] = None
    progress_was_disabled = False
    try:
        from transformers.utils import logging as tr_logging

        prev_verbosity = tr_logging.get_verbosity()
        tr_logging.set_verbosity_error()
        tr_logging.disable_progress_bar()
        progress_was_disabled = True
    except Exception:
        tr_logging = None

    logger_names = (
        "huggingface_hub",
        "huggingface_hub.utils",
        "transformers",
        "transformers.utils.loading_report",
        "sentence_transformers",
    )
    saved_levels: List[Tuple[logging.Logger, int]] = []
    for name in logger_names:
        lg = logging.getLogger(name)
        saved_levels.append((lg, lg.level))
        lg.setLevel(logging.ERROR)

    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r".*unauthenticated requests to the HF Hub.*",
                category=UserWarning,
            )
            warnings.filterwarnings(
                "ignore",
                message=r".*cache-system uses symlinks.*",
                category=UserWarning,
            )
            yield
    finally:
        for lg, lvl in saved_levels:
            lg.setLevel(lvl)
        if tr_logging is not None and prev_verbosity is not None:
            tr_logging.set_verbosity(prev_verbosity)
        if progress_was_disabled and tr_logging is not None:
            tr_logging.enable_progress_bar()


class KnowledgeManager:
    """知识库管理器"""
    
    def __init__(self, config_dir: str, embedding_model: str = DEFAULT_LOCAL_EMBEDDING_MODEL):
        """
        初始化知识库管理器
        Args:
            config_dir: 配置文件目录
            embedding_model: Sentence-Transformers 模型名（Hugging Face id，本地推理）
        """
        if not KNOWLEDGE_AVAILABLE:
            raise ImportError("知识库功能不可用，请安装相关依赖")
            
        self.config_dir = Path(config_dir)
        self.knowledge_dir = self.config_dir / "knowledge"
        self.db_dir = self.config_dir / "knowledge_db"
        self.embedding_model = embedding_model
        
        # 确保目录存在
        self.knowledge_dir.mkdir(exist_ok=True)
        self.db_dir.mkdir(exist_ok=True)
        
        # 初始化Chroma数据库
        self._init_chroma_db()
        
        # 初始化文本分割器
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=1000,
            chunk_overlap=200,
            length_function=len,
            separators=["\n\n", "\n", "。", "！", "？", ".", "!", "?", " ", ""]
        )
        
        # 支持的文件类型
        self.supported_extensions = {
            '.txt': TextLoader,
            '.md': UnstructuredMarkdownLoader,
            '.pdf': PDFMinerLoader,
            '.docx': Docx2txtLoader,
            '.csv': UnstructuredCSVLoader,
            '.xlsx': UnstructuredExcelLoader,
            '.xls': UnstructuredExcelLoader,
            '.json': TextLoader,
            '.py': TextLoader,
            '.js': TextLoader,
            '.html': TextLoader,
            '.htm': TextLoader,
            '.xml': TextLoader,
            '.yaml': TextLoader,
            '.yml': TextLoader,
            '.ini': TextLoader,
            '.cfg': TextLoader,
            '.conf': TextLoader,
            '.log': TextLoader
        }
        
        # 文档状态记录文件
        self.status_file = self.config_dir / "knowledge_status.json"
        self.document_status = self._load_document_status()
        
    def _init_chroma_db(self):
        """初始化Chroma数据库（本地 Sentence-Transformers 嵌入，经 Chroma 统一索引与查询）"""
        try:
            with _quiet_hf_transformers_embedding_init():
                self._embedding_fn = SentenceTransformerEmbeddingFunction(
                    model_name=self.embedding_model
                )
            # 初始化Chroma客户端
            self.client = chromadb.PersistentClient(
                path=str(self.db_dir),
                settings=Settings(
                    anonymized_telemetry=False,
                    allow_reset=True
                )
            )
            
            # 获取或创建集合（嵌入函数与入库/检索一致）
            self.collection = self.client.get_or_create_collection(
                name="smart_shell_knowledge",
                embedding_function=self._embedding_fn,
                metadata={"hnsw:space": "cosine"}
            )
            
            _kb_log.info("Chroma 与嵌入模型就绪，embedding_model=%s", self.embedding_model)
            
        except Exception as e:
            _kb_log.exception("知识库 Chroma/嵌入初始化失败: %s", e)
            raise
    
    def _load_document_status(self) -> Dict[str, Any]:
        """加载文档状态记录"""
        if self.status_file.exists():
            try:
                with open(self.status_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                _kb_log.warning("加载文档状态失败: %s", e)
        return {}
    
    def _save_document_status(self):
        """保存文档状态记录"""
        try:
            with open(self.status_file, 'w', encoding='utf-8') as f:
                json.dump(self.document_status, f, ensure_ascii=False, indent=2)
        except Exception as e:
            _kb_log.warning("保存文档状态失败: %s", e)
    
    def _get_file_hash(self, file_path: Path) -> str:
        """获取文件的MD5哈希值"""
        hash_md5 = hashlib.md5()
        try:
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(4096), b""):
                    hash_md5.update(chunk)
            return hash_md5.hexdigest()
        except Exception:
            return ""
    
    def _get_file_info(self, file_path: Path) -> Dict[str, Any]:
        """获取文件信息"""
        try:
            stat = file_path.stat()
            return {
                "path": str(file_path),
                "name": file_path.name,
                "size": stat.st_size,
                "modified_time": stat.st_mtime,
                "hash": self._get_file_hash(file_path)
            }
        except Exception:
            return {}
    
    def _load_document(self, file_path: Path) -> Optional[str]:
        """加载文档内容"""
        try:
            extension = file_path.suffix.lower()
            if extension not in self.supported_extensions:
                return None
            
            loader_class = self.supported_extensions[extension]
            
            # 特殊处理某些文件类型
            if extension in ['.txt', '.py', '.js', '.html', '.htm', '.xml', '.yaml', '.yml', '.ini', '.cfg', '.conf', '.log', '.json']:
                # 文本文件直接读取
                with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                    return f.read()
            else:
                # 使用LangChain加载器（PDF 经 pdfminer，部分文件会触发 FontBBox 无害警告）
                if extension == ".pdf":
                    with _suppress_pdfminer_font_warnings():
                        loader = loader_class(str(file_path))
                        documents = loader.load()
                else:
                    loader = loader_class(str(file_path))
                    documents = loader.load()
                return "\n\n".join([doc.page_content for doc in documents])
                
        except Exception as e:
            _kb_log.warning("加载文档失败 %s: %s", file_path, e)
            return None
    
    def _add_document_to_db(self, file_info: Dict[str, Any], content: str):
        """将文档添加到数据库"""
        try:
            # 分割文本
            chunks = self.text_splitter.split_text(content)
            
            # 为每个chunk生成ID
            chunk_ids = [f"{file_info['name']}_{i}" for i in range(len(chunks))]
            
            # 添加元数据
            metadatas = [{
                "source": file_info['name'],
                "file_path": file_info['path'],
                "chunk_index": i,
                "file_size": file_info['size'],
                "modified_time": file_info['modified_time']
            } for i in range(len(chunks))]
            
            # 分批添加到Chroma数据库，避免超时
            batch_size = 10
            for i in range(0, len(chunks), batch_size):
                batch_chunks = chunks[i:i+batch_size]
                batch_ids = chunk_ids[i:i+batch_size]
                batch_metadatas = metadatas[i:i+batch_size]
                
                self.collection.add(
                    documents=batch_chunks,
                    metadatas=batch_metadatas,
                    ids=batch_ids
                )
            
            _kb_log.info("已索引文档: %s，片段数=%s", file_info["name"], len(chunks))
            
        except Exception as e:
            _kb_log.warning("添加文档到数据库失败 %s: %s", file_info["name"], e)
    
    def _remove_document_from_db(self, file_name: str):
        """从数据库中删除文档"""
        try:
            # 查找并删除所有相关的chunk
            results = self.collection.get(
                where={"source": file_name}
            )
            
            if results['ids']:
                self.collection.delete(ids=results['ids'])
                _kb_log.info("已从索引删除文档: %s", file_name)
            
        except Exception as e:
            _kb_log.warning("从数据库删除文档失败 %s: %s", file_name, e)
    
    def sync_knowledge_base(self):
        """同步知识库"""
        _kb_log.info("开始同步知识库，目录=%s", self.knowledge_dir)

        # 向量库与 knowledge_status 不一致时（例如误删 knowledge_db、历史版本索引失败），
        # 若仍保留状态则同步会跳过「未变更」文件，导致集合一直为空、检索无结果。
        try:
            if self.collection.count() == 0 and self.document_status:
                _kb_log.warning(
                    "检测到向量索引为空但仍有 %s 条文档状态记录，将清空状态并重新全量索引",
                    len(self.document_status),
                )
                self.document_status = {}
                self._save_document_status()
        except Exception as e:
            _kb_log.warning("知识库索引一致性检查失败: %s", e)
        
        # 获取当前目录下的所有文件
        current_files = {}
        for file_path in self.knowledge_dir.rglob("*"):
            if file_path.is_file() and file_path.suffix.lower() in self.supported_extensions:
                file_info = self._get_file_info(file_path)
                if file_info:
                    current_files[file_path.name] = file_info
        
        # 检查需要删除的文件
        for file_name in list(self.document_status.keys()):
            if file_name not in current_files:
                _kb_log.info("检测到已删除的文档，从索引移除: %s", file_name)
                self._remove_document_from_db(file_name)
                del self.document_status[file_name]
        
        # 检查需要添加或更新的文件
        for file_name, file_info in current_files.items():
            if file_name not in self.document_status:
                # 新文件
                _kb_log.info("发现新文档: %s", file_name)
                content = self._load_document(Path(file_info['path']))
                if content:
                    self._add_document_to_db(file_info, content)
                    self.document_status[file_name] = file_info
            else:
                # 检查是否需要更新
                old_info = self.document_status[file_name]
                if (file_info['modified_time'] != old_info['modified_time'] or 
                    file_info['hash'] != old_info['hash']):
                    _kb_log.info("发现已更新文档，重新索引: %s", file_name)
                    # 先删除旧版本
                    self._remove_document_from_db(file_name)
                    # 添加新版本
                    content = self._load_document(Path(file_info['path']))
                    if content:
                        self._add_document_to_db(file_info, content)
                        self.document_status[file_name] = file_info
        
        # 保存状态
        self._save_document_status()
        
        # 显示统计信息
        total_docs = len(self.document_status)
        total_chunks = self.collection.count()
        _kb_log.info("知识库同步完成: %s 个文档, %s 个文本片段", total_docs, total_chunks)
        if total_docs > 0 and total_chunks == 0:
            _kb_log.error(
                "知识库异常：有文档记录但向量片段为 0，请检查 PDF/加载日志或删除 knowledge_db 与 knowledge_status.json 后执行 /knowledge sync"
            )
    
    def search_knowledge(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """
        搜索知识库
        Args:
            query: 查询文本
            top_k: 返回结果数量
        Returns:
            搜索结果列表
        """
        try:
            if not query.strip():
                return []
            
            # 执行搜索
            results = self.collection.query(
                query_texts=[query],
                n_results=top_k
            )
            
            # 格式化结果
            formatted_results = []
            if results['documents'] and results['documents'][0]:
                for i, doc in enumerate(results['documents'][0]):
                    metadata = results['metadatas'][0][i] if results['metadatas'] and results['metadatas'][0] else {}
                    # 将距离转换为相似度：cosine距离越小，相似度越高
                    # cosine距离范围是0-2，0表示完全相似，2表示完全不相似
                    distance = results['distances'][0][i] if results['distances'] and results['distances'][0] else 1.0
                    similarity = 1.0 - (distance / 2.0)  # 转换为0-1的相似度
                    
                    formatted_results.append({
                        'content': doc,
                        'source': metadata.get('source', 'unknown'),
                        'file_path': metadata.get('file_path', ''),
                        'chunk_index': metadata.get('chunk_index', 0),
                        'similarity': similarity
                    })
            
            return formatted_results
            
        except Exception as e:
            _kb_log.warning("知识库搜索失败: %s", e)
            return []
    
    def get_knowledge_context(self, query: str, max_length: int = 2000) -> str:
        """
        获取知识库上下文
        Args:
            query: 查询文本
            max_length: 最大上下文长度
        Returns:
            格式化的上下文字符串
        """
        results = self.search_knowledge(query, top_k=5)
        
        if not results:
            return ""
        
        # 过滤相似度过低的结果（相似度阈值设为0.3）
        filtered_results = [r for r in results if r['similarity'] >= 0.3]
        
        if not filtered_results:
            return ""
        
        context_parts = []
        current_length = 0
        
        for result in filtered_results:
            content = result['content']
            source = result['source']
            
            # 估算长度（中文字符按2个字符计算）
            content_length = len(content.encode('utf-8'))
            
            if current_length + content_length > max_length:
                if len(context_parts) == 0:
                    context_parts.append(f"【来源: {source}】\n{content[:max_length]}")
                    current_length += max_length
                break
            
            context_parts.append(f"【来源: {source}】\n{content}")
            current_length += content_length
        
        if context_parts:
            return "\n\n".join(context_parts)
        else:
            return ""
    
    def get_knowledge_stats(self) -> Dict[str, Any]:
        """获取知识库统计信息"""
        try:
            total_chunks = self.collection.count()
            total_docs = len(self.document_status)
            
            # 按文件类型统计
            file_types = {}
            for file_name in self.document_status.keys():
                ext = Path(file_name).suffix.lower()
                file_types[ext] = file_types.get(ext, 0) + 1
            
            return {
                "total_documents": total_docs,
                "total_chunks": total_chunks,
                "file_types": file_types,
                "supported_extensions": list(self.supported_extensions.keys()),
                "embedding_model": self.embedding_model,
            }
        except Exception as e:
            _kb_log.warning("获取知识库统计信息失败: %s", e)
            return {}

    def close(self) -> None:
        """Release Chroma resources before the workspace directory is removed or switched."""
        client = getattr(self, "client", None)
        self.collection = None
        self.client = None

        candidates: List[Any] = []
        if client is not None:
            candidates.append(client)
            identifier = getattr(client, "_identifier", None)
            for attr in ("_system", "_server"):
                obj = getattr(client, attr, None)
                if obj is not None:
                    candidates.append(obj)
            server = getattr(client, "_server", None)
            manager = getattr(server, "_manager", None) if server is not None else None
            if manager is not None:
                candidates.append(manager)
                instances = getattr(manager, "_instances", None)
                if isinstance(instances, dict):
                    for instance in list(instances.values()):
                        stop = getattr(instance, "stop", None)
                        if callable(stop):
                            try:
                                stop()
                            except Exception:
                                _kb_log.debug("Chroma segment instance stop failed", exc_info=True)
                    instances.clear()
                segment_cache = getattr(manager, "segment_cache", None)
                if isinstance(segment_cache, dict):
                    for cache in segment_cache.values():
                        reset = getattr(cache, "reset", None)
                        if callable(reset):
                            try:
                                reset()
                            except Exception:
                                _kb_log.debug("Chroma segment cache reset failed", exc_info=True)
            try:
                from chromadb.api.shared_system_client import SharedSystemClient

                system_cache = getattr(SharedSystemClient, "_identifier_to_system", None)
                if isinstance(system_cache, dict):
                    for key in (identifier, str(self.db_dir)):
                        if key is None:
                            continue
                        system = system_cache.pop(key, None)
                        if system is not None:
                            candidates.append(system)
            except Exception:
                _kb_log.debug("Chroma shared system cache cleanup failed", exc_info=True)

        seen = set()
        for obj in candidates:
            ident = id(obj)
            if ident in seen:
                continue
            seen.add(ident)
            for method_name in ("close", "stop", "shutdown"):
                method = getattr(obj, method_name, None)
                if not callable(method):
                    continue
                try:
                    method()
                except Exception:
                    _kb_log.debug("Chroma cleanup method %s failed", method_name, exc_info=True)
                break

        gc.collect()


class KnowledgeService:
    """
    在单一线程池工作线程上执行全部 KnowledgeManager 操作（初始化、同步、检索）。
    Chroma 使用 SQLite 持久化时，要求在创建客户端的线程内访问；若在后台线程初始化、在主线程 query，会导致检索为空或异常。
    """

    def __init__(self, config_dir: str, embedding_model: str = DEFAULT_LOCAL_EMBEDDING_MODEL):
        self._config_dir = str(Path(config_dir))
        self._embedding_model = embedding_model
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="smartshell-kb"
        )
        self._km: Optional[KnowledgeManager] = None
        self._ready = threading.Event()
        self._executor.submit(self._bootstrap)

    def _bootstrap(self) -> None:
        try:
            _kb_log.info("知识库工作线程开始初始化, config_dir=%s", self._config_dir)
            km = KnowledgeManager(self._config_dir, self._embedding_model)
            km.sync_knowledge_base()
            self._km = km
            _kb_log.info("知识库工作线程初始化与同步完成")
        except Exception:
            _kb_log.exception("知识库工作线程初始化失败")
            self._km = None
        finally:
            self._ready.set()

    def wait_ready(self, timeout: float = 600.0) -> bool:
        return self._ready.wait(timeout=timeout)

    def is_ready(self) -> bool:
        return self._ready.is_set()

    def is_available(self) -> bool:
        """初始化已结束且 KnowledgeManager 构建成功。"""
        if not self.is_ready():
            return False
        return self._km is not None

    def sync_knowledge_base(self) -> None:
        def _do() -> None:
            if self._km is None:
                return
            self._km.sync_knowledge_base()

        if not self.wait_ready(600.0):
            return
        if self._km is None:
            return
        self._executor.submit(_do).result(timeout=600.0)

    def search_knowledge(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        def _do() -> List[Dict[str, Any]]:
            if self._km is None:
                return []
            return self._km.search_knowledge(query, top_k)

        if not self.wait_ready(600.0):
            return []
        if self._km is None:
            return []
        return self._executor.submit(_do).result(timeout=120.0)

    def get_knowledge_stats(self) -> Dict[str, Any]:
        def _do() -> Dict[str, Any]:
            if self._km is None:
                return {}
            return self._km.get_knowledge_stats()

        if not self.wait_ready(600.0):
            return {}
        if self._km is None:
            return {}
        return self._executor.submit(_do).result(timeout=60.0)

    def get_knowledge_context(self, query: str, max_length: int = 2000) -> str:
        def _do() -> str:
            if self._km is None:
                return ""
            return self._km.get_knowledge_context(query, max_length)

        if not self.wait_ready(600.0):
            return ""
        if self._km is None:
            return ""
        return self._executor.submit(_do).result(timeout=120.0)

    def shutdown(self, wait: bool = False) -> None:
        """释放 KnowledgeManager/Chroma 资源和线程池。"""
        def _do() -> None:
            km = self._km
            self._km = None
            if km is not None:
                km.close()

        try:
            future = self._executor.submit(_do)
            if wait:
                future.result(timeout=120.0)
        except Exception:
            _kb_log.debug("KnowledgeService shutdown cleanup failed", exc_info=True)
        finally:
            self._executor.shutdown(wait=wait)
