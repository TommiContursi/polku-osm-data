import java.io.File;

import btools.mapaccess.PhysicalFile;
import btools.mapaccess.Rd5DiffTool;
import btools.util.ProgressListener;

/**
 * BRouter's own update and check of one routing square, as its Android app's DownloadWorker does them.
 *
 *   java -cp brouter-1.7.10-all.jar:CLASSES Rd5 check SQUARE.rd5
 *   java -cp brouter-1.7.10-all.jar:CLASSES Rd5 apply OLD.rd5 CHANGE.df5 NEW.rd5
 *
 * A failed check exits non-zero: PhysicalFile.main prints the error and exits 0.
 */
public class Rd5 {
  public static void main(String[] args) throws Exception {
    File square = new File(args[args.length - 1]);
    if (args[0].equals("apply") && args.length == 4) {
      Rd5DiffTool.recoverFromDelta(new File(args[1]), new File(args[2]), square, new ProgressListener() {
        public void updateProgress(String task, int progress) {}
        public boolean isCanceled() { return false; }
      });
    } else if (!args[0].equals("check") || args.length != 2) {
      throw new IllegalArgumentException("Rd5 check SQUARE | Rd5 apply OLD CHANGE NEW");
    }
    PhysicalFile.checkFileIntegrity(square);
  }
}
