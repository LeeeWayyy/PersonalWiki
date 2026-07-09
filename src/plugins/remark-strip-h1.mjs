// remark-strip-h1 — remove the first depth-1 heading from a page body. The page
// title is rendered by the Astro template's title block, so the vault's `# H1`
// would duplicate it.
export default function remarkStripH1() {
  return (tree) => {
    const i = tree.children.findIndex((n) => n.type === 'heading' && n.depth === 1);
    if (i !== -1) tree.children.splice(i, 1);
  };
}
