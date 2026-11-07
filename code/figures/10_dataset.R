## 10_dataset.R -- Fig. 2 (corpus) and Fig. 3 (alignment + similarity)
source("paper/cs_paper/R/00_theme.R")
D <- "paper/cs_paper/data"
MON <- c("Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec")

reg <- read.csv(file.path(D, "d_registry.csv"), stringsAsFactors = FALSE)
DISP <- c(official = "Official", smithery = "Smithery", glama = "Glama",
         modelscope = "ModelScope", mcpso = "mcp.so", aibase = "AIBase", cursor = "Cursor")
reg$name <- factor(DISP[reg$registry], levels = DISP[reg$registry[order(reg$entries)]])
pa <- ggplot(reg, aes(y = name)) +
  geom_segment(aes(x = indexed_repos, xend = entries, yend = name), linewidth = 0.5, colour = "grey55") +
  geom_point(aes(x = entries, colour = "Registry entries"), size = 1.5) +
  geom_point(aes(x = indexed_repos, colour = "Repositories indexed"), size = 1.5) +
  geom_text(aes(x = entries, label = format(entries, big.mark = ","),
                hjust = ifelse(entries > 8e4, 1, -0.22),
                vjust = ifelse(entries > 8e4, -1.2, 0.5)),
            size = 1.8, colour = "grey15") +
  geom_text(aes(x = indexed_repos, label = format(indexed_repos, big.mark = ",")), hjust = 1.22, size = 1.8, colour = "grey15") +
  scale_x_log10(limits = c(100, 1e5), breaks = c(1e2, 1e3, 1e4, 1e5),
                labels = c("100", "1,000", "10,000", "100,000"),
                expand = expansion(mult = c(0, 0))) +
  scale_colour_manual(values = c("Registry entries" = PAL("orange"),
                                 "Repositories indexed" = PAL("blue")), name = NULL) +
  labs(x = "Count (log scale)", y = NULL) +
  theme_cs() + theme(legend.position = "inside", legend.position.inside = c(0.80, 0.13),
                     legend.direction = "vertical", legend.key.height = unit(6, "pt"))

fd <- read.csv(file.path(D, "d_fetchday.csv"), stringsAsFactors = FALSE)
fd$date <- as.Date(fd$day)
fd$lab  <- paste0(format(fd$date, "%d"), " ", MON[as.integer(format(fd$date, "%m"))])
fd$lab  <- factor(fd$lab, levels = fd$lab); fd$highlight <- factor(fd$day == "2026-09-21")
pb <- ggplot(fd, aes(lab, snapshots)) +
  geom_segment(aes(xend = lab, y = 40, yend = snapshots), linewidth = 0.5, colour = "grey55") +
  geom_point(aes(colour = highlight), size = 1.7) +
  geom_text(aes(label = format(snapshots, big.mark = ",")), vjust = -0.75, size = 1.75, colour = "grey15") +
  annotate("text", x = 5.2, y = 3.4e5, label = "only 21 Sep has\nsame-day cross-source units",
           size = 1.85, colour = PAL("vermillion"), lineheight = 0.95, hjust = 0.5) +
  scale_colour_manual(values = c("FALSE" = PAL("sky"), "TRUE" = PAL("vermillion")), guide = "none") +
  scale_y_log10(limits = c(40, 6e5), breaks = c(1e2, 1e3, 1e4, 1e5),
                labels = c("100", "1,000", "10,000", "100,000")) +
  labs(x = "Fetch day", y = "Registry snapshots (log scale)") +
  theme_cs() + theme(axis.text.x = element_text(angle = 45, hjust = 1))

p2 <- pa + pb + plot_annotation(tag_levels = "a", tag_prefix = "(", tag_suffix = ")") &
  theme(plot.tag = element_text(size = BASE_PT + 1.5, face = "bold"))
save_fig(p2, "fig03_corpus", W2, MM(70))

fun <- read.csv(file.path(D, "d_funnel.csv"), stringsAsFactors = FALSE)
labs_en <- c(repo_index = "Index rows", "唯一货号" = "Repositories",
             "跨源对齐单元" = "Cross-source units",
             "描述可比" = "Comparable\ndescriptions", different = "Divergent\ndescriptions")
fun$stage <- factor(fun$stage, levels = rev(c("repo_index", "唯一货号", "跨源对齐单元", "描述可比", "different")))
pa3 <- ggplot(fun, aes(value, stage)) +
  geom_col(fill = PAL("blue"), colour = "black", linewidth = 0.25, width = 0.62) +
  geom_text(aes(x = ifelse(value > 120000, value - 1200, value),
                label = paste0(format(value, big.mark = ","), "   (",
                               sprintf("%.1f", 100 * value / max(value)), "%)"),
                hjust = ifelse(value > 120000, 1.05, -0.06),
                colour = ifelse(value > 120000, "white", "grey15")),
            size = 1.9, show.legend = FALSE) +
  scale_y_discrete(labels = labs_en) +
  scale_x_continuous(limits = c(0, 150000), breaks = seq(0, 150000, 50000),
                     labels = label_number(big.mark = ","),
                     expand = expansion(mult = c(0, 0))) +
  scale_colour_identity() +
  labs(x = "Units", y = NULL) + theme_cs()

sim <- read.csv(file.path(D, "d_sim.csv"), stringsAsFactors = FALSE)
sim$similarity <- as.numeric(sim$similarity)
pa3b <- ggplot(sim, aes(similarity)) +
  geom_histogram(aes(fill = after_stat(x < 0.5)), binwidth = 0.025, colour = NA, boundary = 0) +
  geom_vline(xintercept = c(0.5, 0.9), linetype = "dashed", linewidth = 0.3, colour = "grey30") +
  annotate("text", x = 0.24, y = 470, label = "Divergent\n(similarity < 0.5)\n5,573 units", size = 1.95,
           colour = PAL("vermillion"), lineheight = 0.95, hjust = 0.5) +
  annotate("text", x = 0.56, y = 320, label = "Partial", size = 1.95, hjust = 0) +
  annotate("text", x = 0.88, y = 320, label = "Near-duplicate /\nidentical", size = 1.95, hjust = 1, lineheight = 0.95) +
  annotate("text", x = 0.5, y = 470, label = "\u03c4 = 0.5", size = 1.8, colour = "grey25", hjust = -0.15) +
  annotate("text", x = 0.972, y = 410, hjust = 1, lineheight = 0.95, size = 1.55,
           label = "366 units at \u2265 0.9\n(204 identical,\n162 near-duplicate)\n260 at exactly 1.00") +
  scale_fill_manual(values = c("TRUE" = PAL("vermillion"), "FALSE" = PAL("grey")), guide = "none") +
  scale_x_continuous(limits = c(0, 1), breaks = seq(0, 1, 0.25), expand = expansion(mult = c(0.005, 0.01))) +
  scale_y_continuous(limits = c(0, 500), breaks = seq(0, 500, 100),
                     labels = label_number(big.mark = ","),
                     expand = expansion(mult = c(0, 0))) +
  labs(x = "Maximum pairwise description similarity", y = "Cross-source units") + theme_cs()

p3 <- pa3 + pa3b + plot_annotation(tag_levels = "a", tag_prefix = "(", tag_suffix = ")") &
  theme(plot.tag = element_text(size = BASE_PT + 1.5, face = "bold"))
save_fig(p3, "fig04_alignment", W2, MM(68))
