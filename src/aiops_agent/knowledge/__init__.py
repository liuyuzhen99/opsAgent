__all__ = ["KnowledgeEngine"]


def __getattr__(name: str):
    if name == "KnowledgeEngine":
        from aiops_agent.knowledge.engine import KnowledgeEngine
        return KnowledgeEngine
    raise AttributeError(name)
