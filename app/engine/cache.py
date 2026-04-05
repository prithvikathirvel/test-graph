class GraphCache:
    """In-memory cache to store compiled LangGraphs."""
    _graphs = {}

    @classmethod
    def set(cls, agent_id: str, compiled_graph):
        cls._graphs[agent_id] = compiled_graph

    @classmethod
    def get(cls, agent_id: str):
        return cls._graphs.get(agent_id)