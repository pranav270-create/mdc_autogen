import astroid
from astroid import nodes
import logging
import os
import networkx as nx

# Import from our new modules
from .llm_utils.llm_client import generate_mdc_response, batch_generate_mdc_responses
from .llm_utils.prompts import (
    format_file_prompt,
    format_directory_prompt,
    format_repository_prompt,
    SYSTEM_PROMPT,
)


def read_file_content(file_path):
    """Read the content of a file with proper encoding handling."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        try:
            # Try with another encoding if utf-8 fails
            with open(file_path, "r", encoding="latin-1") as f:
                return f.read()
        except Exception:
            logging.error("Error reading file {}: {}".format(file_path, e))
            return None


def split_content(content, file_path):
    """Split Python file content into functions, classes, and free-standing code."""
    # Handle Python files
    if file_path.endswith(".py"):
        try:
            module = astroid.parse(content)
            splits = []
            current_block = []

            for node in module.body:
                if isinstance(node, (nodes.FunctionDef, nodes.ClassDef)):
                    if current_block:
                        splits.append(
                            {
                                "name": "free_standing_code",
                                "type": "code",
                                "content": "\n".join(current_block),
                            }
                        )
                        current_block = []
                    splits.append(
                        {
                            "name": node.name,
                            "type": type(node).__name__,
                            "content": node.as_string(),
                        }
                    )
                else:
                    current_block.append(node.as_string())

            if current_block:
                splits.append(
                    {
                        "name": "free_standing_code",
                        "type": "code",
                        "content": "\n".join(current_block),
                    }
                )
            return (
                splits
                if splits
                else [{"name": "whole_file", "type": "file", "content": content}]
            )
        except Exception:
            # If parsing fails, return the whole content
            return [{"name": "whole_file", "type": "file", "content": content}]
    # Handle JavaScript and TypeScript files
    elif file_path.endswith((".js", ".jsx", ".ts", ".tsx")):
        pass
    else:
        # For all other file types, just include them as whole files
        return [{"name": "whole_file", "type": "file", "content": content}]


async def generate_directory_mdc(
    directory,
    file_snippets_dict,
    dependency_graph,
    output_dir,
    model_name="gpt-4o-mini",
    temperature=0.3,
):
    """
    Generate an MDC file for a directory, including dependency information.

    Args:
        directory: Directory path to document
        file_snippets_dict: Dictionary of code snippets for files in this directory
        dependency_graph: NetworkX DiGraph with dependency information
        output_dir: Directory to write the MDC file
        model_name: OpenAI model to use
        temperature: Temperature for generation

    Returns:
        Path to the generated MDC file
    """
    try:
        # Create directory-specific MDC path
        dir_mdc_path = os.path.join(output_dir, directory, "_directory.mdc")
        os.makedirs(os.path.dirname(dir_mdc_path), exist_ok=True)

        # Find files in this directory
        dir_files = [
            f for f in file_snippets_dict.keys() if os.path.dirname(f) == directory
        ]

        # Get dependency information for the directory as a whole
        dir_imports = set()
        imported_by_dir = set()

        for file_path in dir_files:
            if file_path in dependency_graph.nodes():
                # Files outside this directory that are imported by files in this directory
                dir_imports.update(
                    [
                        succ
                        for succ in dependency_graph.successors(file_path)
                        if os.path.dirname(succ) != directory
                    ]
                )

                # Files outside this directory that import files in this directory
                imported_by_dir.update(
                    [
                        pred
                        for pred in dependency_graph.predecessors(file_path)
                        if os.path.dirname(pred) != directory
                    ]
                )

        # Build the prompt with directory content and dependencies
        user_prompt = format_directory_prompt(
            directory, dir_files, dir_imports, imported_by_dir
        )

        # Generate MDC content
        response = await generate_mdc_response(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            model_name=model_name,
            temperature=temperature,
        )

        # Write MDC file
        write_mdc_file(dir_mdc_path, response)
        return dir_mdc_path

    except Exception as e:
        logging.error("Error generating directory MDC for {}: {}".format(directory, e))
        return None


async def generate_high_level_mdc(
    repo_path,
    file_snippets_dict,
    dependency_graph,
    output_dir,
    model_name="gpt-4o-mini",
    temperature=0.3,
):
    """
    Generate a high-level MDC file for the entire repository.

    Args:
        repo_path: Path to the repository
        file_snippets_dict: Dictionary of code snippets for all files
        dependency_graph: NetworkX DiGraph with dependency information
        output_dir: Directory to write the MDC file
        model_name: OpenAI model to use
        temperature: Temperature for generation

    Returns:
        Path to the generated MDC file
    """
    try:
        # Create repository-level MDC path
        repo_mdc_path = os.path.join(output_dir, "_repository.mdc")
        os.makedirs(os.path.dirname(repo_mdc_path), exist_ok=True)

        # Get top-level directories
        directories = {
            os.path.dirname(f) for f in file_snippets_dict.keys() if os.path.dirname(f)
        }

        # Analyze core modules (most imported files)
        in_degree = sorted(
            [(n, dependency_graph.in_degree(n)) for n in dependency_graph.nodes()],
            key=lambda x: x[1],
            reverse=True,
        )
        core_modules = [
            (n, d) for n, d in in_degree if d >= 3
        ]  # Files imported by at least 3 other files

        # Try to identify entry points
        entry_points = [
            n
            for n in dependency_graph.nodes()
            if dependency_graph.out_degree(n) > 0 and dependency_graph.in_degree(n) == 0
        ]

        # Identify circular dependencies
        cycles = []
        try:
            cycles = list(nx.simple_cycles(dependency_graph))
        except Exception:
            pass

        # Build the prompt with repository content and dependency information
        user_prompt = format_repository_prompt(
            directories, core_modules, entry_points, cycles
        )

        # Generate MDC content
        response = await generate_mdc_response(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            model_name=model_name,
            temperature=temperature,
        )

        # Write MDC file
        write_mdc_file(repo_mdc_path, response)
        return repo_mdc_path

    except Exception as e:
        logging.error("Error generating repository MDC: {}".format(e))
        return None


def write_mdc_file(output_path, mdc_content):
    """
    Write the MDC content to a file.

    Args:
        output_path: Path to write the MDC file
        mdc_content: MDCResponse object with the content
    """
    try:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        with open(output_path, "w", encoding="utf-8") as f:
            # Write the frontmatter
            f.write("---\n")
            f.write("description: {}\n".format(mdc_content.description))
            f.write("globs: {}\n".format(mdc_content.globs))
            f.write("alwaysApply: {}\n".format(str(mdc_content.always_apply).lower()))
            f.write("---\n\n")

            # Write the content
            f.write(mdc_content.content)

        logging.info("MDC file created: {}".format(output_path))
    except Exception as e:
        logging.error("Error writing MDC file {}: {}".format(output_path, e))


async def generate_mdc_files(
    file_data,
    dependency_graph,
    output_dir="output/.cursor/rules",
    model_name="gpt-4o-mini",
    include_import_rules=False,
    skip_directory_mdcs=False,
    skip_repository_mdc=False,
    max_directory_depth=2,
):
    """
    Generate MDC files for all files, directories, and the repository.

    Args:
        file_data: Dictionary of file snippets
        dependency_graph: NetworkX DiGraph with dependency information
        output_dir: Directory to write the MDC files
        model_name: OpenAI model to use
        include_import_rules: If True, include @file references to imported files in MDC content
        skip_directory_mdcs: If True, skip generating directory-level MDC files
        skip_repository_mdc: If True, skip generating repository-level MDC file
        max_directory_depth: Maximum directory depth for generating MDC files (0=repo only, 1=top-level dirs, etc.)

    Returns:
        List of paths to generated MDC files
    """
    mdc_files = []

    # Create output directory
    os.makedirs(output_dir, exist_ok=True)

    # Batch preparation for file-specific MDCs
    file_prompts = []
    file_paths = []

    for file_path, snippets in file_data.items():
        if not snippets:  # Skip files with no content
            continue

        # Get dependency information
        imported_by = []
        imports = []

        if file_path in dependency_graph.nodes():
            # Files that import this file
            imported_by = [pred for pred in dependency_graph.predecessors(file_path)]
            # Files that this file imports
            imports = [succ for succ in dependency_graph.successors(file_path)]

        # Build the prompt with file content and dependencies
        user_prompt = format_file_prompt(file_path, snippets, imports, imported_by)
        file_prompts.append(
            {"system_prompt": SYSTEM_PROMPT, "user_prompt": user_prompt}
        )
        file_paths.append(file_path)

    # Batch generate file MDCs
    file_responses = await batch_generate_mdc_responses(
        prompts=file_prompts, model_name=model_name, batch_size=10
    )

    # Write MDC files for files
    for file_path, response in zip(file_paths, file_responses):
        if response:
            mdc_path = os.path.join(output_dir, "{}.mdc".format(file_path))

            if include_import_rules and file_path in dependency_graph.nodes():
                imports = [succ for succ in dependency_graph.successors(file_path)]
                if imports:
                    import_references = "\n\n## Imported Files\n"
                    for imported_file in imports:
                        import_references += "@file .cursor/rules/{}.mdc\n".format(
                            imported_file
                        )

                    # Append the import references to the content
                    response.content += import_references

            write_mdc_file(mdc_path, response)
            mdc_files.append(mdc_path)

    # Generate directory MDCs if not skipped
    if not skip_directory_mdcs and max_directory_depth > 0:
        # Get all directories from file paths
        all_directories = {
            os.path.dirname(f) for f in file_data.keys() if os.path.dirname(f)
        }

        # Filter directories based on depth
        directories_to_process = []
        for directory in all_directories:
            # Calculate directory depth (number of path separators)
            depth = directory.count(os.path.sep) + 1
            if depth <= max_directory_depth:
                directories_to_process.append(directory)

        logging.info(
            f"Generating MDC files for {len(directories_to_process)} directories (max depth: {max_directory_depth})"
        )

        for directory in directories_to_process:
            mdc_path = await generate_directory_mdc(
                directory, file_data, dependency_graph, output_dir, model_name
            )
            if mdc_path:
                mdc_files.append(mdc_path)
    elif skip_directory_mdcs:
        logging.info("Skipping directory MDC generation as requested")
    else:
        logging.info(f"No directories within max depth of {max_directory_depth}")

    # Generate high-level repository MDC if not skipped
    if not skip_repository_mdc:
        repo_mdc_path = await generate_high_level_mdc(
            os.path.dirname(list(file_data.keys())[0]) if file_data else ".",
            file_data,
            dependency_graph,
            output_dir,
            model_name,
        )
        if repo_mdc_path:
            mdc_files.append(repo_mdc_path)
    else:
        logging.info("Skipping repository MDC generation as requested")

    return mdc_files
