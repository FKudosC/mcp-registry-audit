# Figure 7: the drift census.
#
# (a) what the re-probe found for the 14,797 frozen units
# (b) how far the default branch had advanced, for the units whose diff GitHub returned
#
# Data: out/fig7_data.json, written by out/export_drift_fig_data.py
# Run:  Rscript paper/csea/figures/make_drift_fig.R
#
# Layout note: no text sits inside the plotting area. The 36.5% summary and the median are
# carried by the axis labels and the caption, because at 160 mm a panel half this width has
# no free space and in-panel annotations collide with the bars.

suppressPackageStartupMessages({
  library(ggplot2)
  library(patchwork)
  library(jsonlite)
  library(scales)
  library(svglite)
  library(ragg)
})

FIG <- file.path("paper", "csea", "figures")
DATA <- file.path("paper", "csea", "out", "fig7_data.json")
if (!file.exists(DATA)) DATA <- file.path("..", "out", "fig7_data.json")
stopifnot(file.exists(DATA))

d <- fromJSON(DATA)
o <- d$outcomes
total <- d$total
resolvable <- o$advanced + o$unchanged
advanced_or_gone <- d$advanced_or_gone

bars <- data.frame(
  outcome = factor(c("Advanced", "Unchanged", "No longer\nexists", "Timeout"),
                   levels = c("Advanced", "Unchanged", "No longer\nexists", "Timeout")),
  units = c(o$advanced, o$unchanged, o$no_longer_exists, o$timeout)
)
bars$share <- ifelse(bars$outcome %in% c("Advanced", "Unchanged"),
                     bars$units / resolvable, bars$units / total)
bars$lab <- sprintf("%s\n(%.1f%%)", comma(bars$units), 100 * bars$share)

panel_a <- ggplot(bars, aes(outcome, units, fill = outcome)) +
  geom_col(width = 0.6, colour = "black", linewidth = 0.4) +
  geom_text(aes(label = lab), vjust = -0.25, size = 3.0, lineheight = 0.95) +
  scale_fill_manual(values = c("Advanced" = "#D2601A",
                               "Unchanged" = "#9AA0A6",
                               "No longer\nexists" = "#B07AA1",
                               "Timeout" = "#EDEDED"), guide = "none") +
  scale_y_continuous(labels = label_comma(),
                     expand = expansion(mult = c(0, 0.20))) +
  labs(x = NULL, y = "Declared-frozen units",
       subtitle = sprintf("%s of %s advanced or gone (%.1f%%)",
                          comma(advanced_or_gone), comma(total),
                          100 * advanced_or_gone / total)) +
  theme_minimal(base_size = 9.5) +
  theme(panel.grid.minor = element_blank(),
        panel.grid.major.x = element_blank(),
        axis.text.x = element_text(size = 8.5, lineheight = 0.9),
        plot.subtitle = element_text(size = 8.5, colour = "grey25"))

ahead <- data.frame(commits = d$ahead_by)
med <- d$ahead_median

panel_b <- ggplot(ahead, aes(commits)) +
  geom_histogram(bins = 14, fill = "#D2601A", colour = "black", linewidth = 0.35) +
  geom_vline(xintercept = med, linetype = "dashed", colour = "grey30", linewidth = 0.45) +
  scale_x_log10(breaks = c(1, 3, 10, 30, 100, 300),
                labels = c("1", "3", "10", "30", "100", "300")) +
  scale_y_continuous(expand = expansion(mult = c(0, 0.18))) +
  labs(x = "Commits advanced (log scale)",
       y = sprintf("Number of cases (n = %d)", nrow(ahead))) +
  theme_minimal(base_size = 9.5) +
  theme(panel.grid.minor = element_blank())

fig <- panel_a + panel_b +
  plot_layout(widths = c(1.25, 1)) +
  plot_annotation(tag_levels = list(c("(a)", "(b)"))) &
  theme(plot.tag = element_text(size = 10.5, face = "bold"),
        plot.margin = margin(3, 6, 3, 3))

dir.create(FIG, showWarnings = FALSE, recursive = TRUE)
stem <- file.path(FIG, "fig7_drift")
w <- 160; h <- 74
ggsave(paste0(stem, ".pdf"), fig, width = w, height = h, units = "mm", device = grDevices::cairo_pdf)
ggsave(paste0(stem, ".png"), fig, width = w, height = h, units = "mm",
       dpi = 1200, device = ragg::agg_png)
ggsave(paste0(stem, ".svg"), fig, width = w, height = h, units = "mm", device = svglite::svglite)
cat("wrote fig7_drift .pdf/.png/.svg\n")
