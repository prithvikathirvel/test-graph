from langgraph.graph import StateGraph, START, END
from functools import partial
import logging
from app.core.state import FlowState
from app.engine.registry import NodeRegistry
from app.utils.templating import resolve_placeholders

logger = logging.getLogger(__name__)

# --- ROUTER FUNCTIONS ---

def decision_router(state: FlowState, node_config: dict) -> str:
    """Simply reads the output variable the Decision Node just saved!"""
    out_params = node_config.get("outputParameters", [])
    output_key = out_params[0]["value"] if out_params else "decision_result"
    
    # The decision_node saved the target here. We just read it and jump!
    return state["variables"].get(output_key, "__END__")

def iterator_router(state: FlowState, node_config: dict) -> str:
    """Reads the iterator status flag and routes."""
    node_id = node_config["node_id"]
    status = state["variables"].get(f"_iter_status_{node_id}", "complete")

    if status == "loop":
        return node_config.get("loopPath")
    else:
        return node_config.get("completePath") or node_config.get("completionPath", "__END__")


# --- GRAPH COMPILER ---

class GraphCompiler:
    def __init__(self, schema: dict, checkpointer=None):
        self.schema = schema
        self.checkpointer = checkpointer
        self.workflow = StateGraph(FlowState)

    def build(self):
        nodes = self.schema.get("graphSpec", {}).get("nodes", [])
        edges = self.schema.get("graphSpec", {}).get("edges", [])

        start_node_id = next((n["node_id"] for n in nodes if n["type"] == "start"), None)
        end_node_ids = [n["node_id"] for n in nodes if n["type"] in ["output", "outputs"] or "End Node" in n["name"]]
        
        decision_nodes = {n["node_id"]: n for n in nodes if n["type"] == "conditions"}
        iterator_nodes = {n["node_id"]: n for n in nodes if n["type"] == "iterator"}

        # 1. ADD ALL NODES TO GRAPH (Including Decision & Iterator!)
        for node in nodes:
            if node["type"] == "start": 
                continue
            
            executor_name = node.get("name", "Unknown")
            # Ensure "Iterator Node" and "Decision Node" names map correctly to the registry
            if node["type"] == "iterator": executor_name = "Iterator Node"
            if node["type"] == "conditions": executor_name = "Decision Node"

            executor = NodeRegistry.get_executor(executor_name)
            bound_executor = partial(executor, node_config=node)
            self.workflow.add_node(node["node_id"], bound_executor)

        # 2. WIRE DECISION NODES
        for dec_node in decision_nodes.values():
            path_map = {"__END__": END}
            conditions = next((p["value"] for p in dec_node["inputParameters"] if p["key"] == "conditions"), [])
            
            for cond in conditions:
                tid = cond.get("nextNode")
                if tid: path_map[tid] = tid # <-- FIX: Let it hit the End node!

            self.workflow.add_conditional_edges(
                dec_node["node_id"], 
                partial(decision_router, node_config=dec_node), 
                path_map
            )

        # 3. WIRE ITERATOR NODES
        for iter_node in iterator_nodes.values():
            loop_path = iter_node.get("loopPath")
            complete_path = iter_node.get("completePath") or iter_node.get("completionPath")
            
            path_map = {"__END__": END}
            if loop_path: path_map[loop_path] = loop_path # <-- FIX
            if complete_path: path_map[complete_path] = complete_path # <-- FIX
            
            self.workflow.add_conditional_edges(
                iter_node["node_id"], 
                partial(iterator_router, node_config=iter_node),
                path_map
            )

        # 4. WIRE STANDARD EDGES
        for edge in edges:
            source, target = edge["from"], edge["to"]
            
            if source == start_node_id:
                self.workflow.add_edge(START, target)
                continue
                
            if source in decision_nodes or source in iterator_nodes:
                continue
                
            # FIX: Wire standard edges normally! Don't hijack them to END.
            self.workflow.add_edge(source, target)

        # 5. ENSURE END NODES TERMINATE
        for e_id in end_node_ids:
            if e_id in self.workflow.nodes:
                # FIX: Now we explicitly say "After the End node finishes running, terminate the graph"
                self.workflow.add_edge(e_id, END)

        return self.workflow.compile(checkpointer=self.checkpointer)